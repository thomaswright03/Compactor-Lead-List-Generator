"""Daily API call budgets and a results cache, kept in the database (store.py).

Render's own disk is wiped whenever the site sleeps or redeploys, so a count
kept there could reset mid-day. The count lives in the permanent database
instead; when it cannot be read or updated, the budget counts as used up.
"""

import datetime as dt
import hashlib
import json
import time

from . import config, store


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _today():
    return _now().strftime("%Y-%m-%d")


def reset_time_text():
    """When a UTC-day budget resets, in Utah time: '6 pm Utah time'."""
    midnight = (_now() + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0,
                                                       microsecond=0)
    try:
        from zoneinfo import ZoneInfo
        local = midnight.astimezone(ZoneInfo("America/Denver"))
    except Exception:
        return "midnight UTC"
    hour = local.hour % 12 or 12
    return f"{hour} {'am' if local.hour < 12 else 'pm'} Utah time"


def _why(exc, what):
    if isinstance(exc, store.Unavailable):
        return str(exc)
    return f"the daily usage counter could not be {what} ({exc.__class__.__name__})"


class DailyBudget:
    """At most `limit` calls per UTC day, counted across every search and restart."""

    def __init__(self, name, limit):
        self.name = name
        self.limit = limit
        self.problem = None          # why the budget cannot be used right now

    def used(self):
        """Calls spent today; the full limit when the count cannot be read."""
        self.problem = None
        try:
            with store.connect() as db:
                row = db.one("SELECT used FROM usage WHERE name = ? AND day = ?",
                             (self.name, _today()))
            return min(self.limit, int(row[0])) if row else 0
        except Exception as exc:
            self.problem = _why(exc, "read")
            return self.limit

    def left(self):
        return max(0, self.limit - self.used())

    def take(self):
        """Reserve one call. False when today's calls are used up or cannot be counted."""
        self.problem = None
        if self.limit < 1:
            return False
        try:
            with store.connect() as db:
                # One atomic statement: counts the call only while under the limit.
                row = db.one("INSERT INTO usage (name, day, used) VALUES (?, ?, 1) "
                             "ON CONFLICT (name, day) DO UPDATE SET used = usage.used + 1 "
                             "WHERE usage.used < ? RETURNING used",
                             (self.name, _today(), self.limit))
            return row is not None
        except Exception as exc:
            self.problem = _why(exc, "updated")
            return False


def yelp_budget():
    return DailyBudget("yelp", config.YELP_DAILY_LIMIT)


class SharedCache:
    """A results cache in the database, so it survives restarts. Misses on any error."""

    def get(self, key, ttl=None):
        try:
            with store.connect() as db:
                row = db.one("SELECT value, expires_at FROM cache WHERE key = ?", (self._key(key),))
            if row and row[1] > time.time():
                return json.loads(row[0])
        except Exception:
            pass
        return None

    def put(self, key, value, ttl=None):
        now = time.time()
        try:
            with store.connect() as db:
                db.run("DELETE FROM cache WHERE expires_at < ?", (now,))
                db.run("INSERT INTO cache (key, value, expires_at) VALUES (?, ?, ?) "
                       "ON CONFLICT (key) DO UPDATE SET value = excluded.value, "
                       "expires_at = excluded.expires_at",
                       (self._key(key), json.dumps(value, separators=(",", ":")),
                        now + (ttl or config.CACHE_TTL_SECONDS)))
        except Exception:
            pass                        # a cache that cannot be written only costs a miss

    @staticmethod
    def _key(key):
        return hashlib.sha256(key.encode()).hexdigest()
