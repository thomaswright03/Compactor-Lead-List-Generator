"""API call budgets and a results cache, kept in the database (store.py).

Render's own disk is wiped whenever the site sleeps or redeploys, so a count
kept there could reset mid-day. The count lives in the permanent database
instead; when it cannot be read or updated, the budget counts as used up.
"""

import datetime as dt
import hashlib
import json
import logging
import random
import time
from collections.abc import Callable
from typing import Any

from . import config, store
from .localtime import day_clock_text

log = logging.getLogger(__name__)

# A call counts for 24 hours after it was made, plus this margin: a request may
# take 60 s, plus a few seconds of retry wait, before it reaches the API.
WINDOW_SECONDS = 24 * 3600 + 90


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _clock() -> float:
    return _now().timestamp()


def _why(exc: BaseException, what: str) -> str:
    if isinstance(exc, store.Unavailable):
        return str(exc)
    log.warning("The Yelp usage counter could not be %s", what, exc_info=exc)
    return f"the usage counter could not be {what}"


class DailyBudget:
    """At most `limit` calls in any 24 hours, counted across every search and restart.

    Each call counts for 24 hours after it was made, so the calls come back 24
    hours after the last search that used them (any 24 hours also covers the
    API's own day, which runs from midnight UTC).
    """

    def __init__(self, name: str, limit: int) -> None:
        self.name = name
        self.limit = limit
        self.problem: str | None = None   # why the budget cannot be used right now
        self._calls: list[float] = []            # times of the calls that still count, from the last read

    def _load(self, db: store.Db) -> tuple[int, list[float]]:
        row = db.one("SELECT version, calls FROM windows WHERE name = ?", (self.name,))
        if row is None:
            # First use: yesterday's style of count (per UTC day) carries over, as calls
            # made now, so switching can never allow more than the limit.
            day = db.one("SELECT used FROM usage WHERE name = ? AND day = ?",
                         (self.name, _now().strftime("%Y-%m-%d")))
            seeded = [_clock()] * min(self.limit, int(day[0])) if day else []
            db.run("INSERT INTO windows (name, version, calls) VALUES (?, 0, ?) "
                   "ON CONFLICT (name) DO NOTHING", (self.name, json.dumps(seeded)))
            row = db.one("SELECT version, calls FROM windows WHERE name = ?", (self.name,))
            if row is None:              # just written; only a broken database gets here
                raise store.Unavailable("the usage counter could not be read")
        cutoff = _clock() - WINDOW_SECONDS
        return int(row[0]), [t for t in json.loads(row[1]) if t > cutoff]

    def used(self) -> int:
        """Calls that still count; the full limit when the count cannot be read."""
        self.problem = None
        try:
            with store.connect() as db:
                _, self._calls = self._load(db)
            return min(self.limit, len(self._calls))
        except Exception as exc:  # noqa: BLE001 - logged by _why; the budget counts as used up
            self.problem = _why(exc, "read")
            return self.limit

    def left(self) -> int:
        return max(0, self.limit - self.used())

    def resets_at(self) -> float | None:
        """When every call counted now is back (from the last used()/left()), or None."""
        return max(self._calls) + WINDOW_SECONDS if self._calls else None

    def reset_text(self) -> str | None:
        at = self.resets_at()
        # Rounded up to the minute; "today at 4:43 PM" or "tomorrow at 4:43 PM".
        return day_clock_text(-(-at // 60) * 60, _now()) if at else None

    def take(self) -> bool:
        """Reserve one call. False when the calls are used up or cannot be counted."""
        self.problem = None
        if self.limit < 1:
            return False
        try:
            with store.connect() as db:
                for _ in range(200):
                    version, calls = self._load(db)
                    if len(calls) >= self.limit:
                        self._calls = calls
                        return False
                    calls.append(_clock())
                    # Compare-and-swap: only one of two simultaneous takes can win a version.
                    won = db.one("UPDATE windows SET version = ?, calls = ? "
                                 "WHERE name = ? AND version = ? RETURNING version",
                                 (version + 1, json.dumps(calls), self.name, version))
                    if won is not None:
                        self._calls = calls
                        return True
                    time.sleep(random.uniform(0, 0.01))
            self.problem = "the usage counter was too busy to update"
            return False
        except Exception as exc:  # noqa: BLE001 - logged by _why; no call is spent
            self.problem = _why(exc, "updated")
            return False


def yelp_budget() -> DailyBudget:
    return DailyBudget("yelp", config.YELP_DAILY_LIMIT)


class SharedCache:
    """A results cache in the database, so it survives restarts, on one connection
    for the whole search (close() it). A failed lookup is retried once on a fresh
    connection; after that the cache counts as down, and `down` says so (the
    caller should then stop spending calls on searches that may be cached)."""

    def __init__(self) -> None:
        self.down = False
        self._db: store.Db | None = None

    def _run(self, fn: Callable[[store.Db], Any]) -> Any:
        for _ in (1, 2):
            try:
                if self._db is None:
                    self._db = store.open_db()
                return fn(self._db)
            except Exception as exc:  # noqa: BLE001 - any database error means "cache down"
                log.warning("The results cache failed: %r", exc)
                self.close()
        self.down = True
        return None

    def close(self) -> None:
        if self._db is not None:
            try:
                self._db.conn.close()
            except Exception as exc:  # noqa: BLE001 - closing a broken connection
                log.info("Closing the cache connection failed: %r", exc)
            self._db = None

    def get(self, key: str, ttl: float | None = None) -> Any:
        if self.down:
            return None
        row = self._run(lambda db: db.one("SELECT value, expires_at FROM cache WHERE key = ?",
                                          (self._key(key),)) or ())
        return json.loads(row[0]) if row and row[1] > time.time() else None

    def put(self, key: str, value: object, ttl: float | None = None) -> None:
        if self.down:
            return
        now = time.time()

        def write(db: store.Db) -> bool:
            db.run("DELETE FROM cache WHERE expires_at < ?", (now,))
            db.run("INSERT INTO cache (key, value, expires_at) VALUES (?, ?, ?) "
                   "ON CONFLICT (key) DO UPDATE SET value = excluded.value, "
                   "expires_at = excluded.expires_at",
                   (self._key(key), json.dumps(value, separators=(",", ":")),
                    now + (ttl or config.CACHE_TTL_SECONDS)))
            return True
        self._run(write)

    @staticmethod
    def _key(key: str) -> str:
        return hashlib.sha256(key.encode()).hexdigest()
