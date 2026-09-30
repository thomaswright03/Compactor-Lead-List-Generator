"""One search a day: Find Leads works once per calendar day, Utah time.

Each day's search is recorded (when it was started, what was searched and what
it found) in the database; the record is claimed atomically when the button is
clicked, so two clicks can never both run. A search that fails before finding
anything (e.g. an unknown location) gives the day back, and so does one where a
source failed (e.g. Google refused its key) even though others found businesses:
what was found is saved, and the day's search can be run again.
"""

import datetime as dt
import json
import time
import uuid
from typing import Any

from . import localtime, store
from .localtime import date_time_text


def _local_now() -> dt.datetime:
    return localtime.now()


def today() -> str:
    return _local_now().strftime("%Y-%m-%d")


# A search takes minutes. One that never finished (the server restarted mid-search)
# stops holding the day after this long, so the day's search can be run again.
STALE_SECONDS = 30 * 60

# A day's search as the page shows it: day, at, when, and what was searched and found.
Record = dict[str, Any]


def claim(info: dict[str, Any]) -> tuple[str | None, Record | None]:
    """Record today's search. Returns (day, None), or (None, today's record) if one ran."""
    day, now = today(), time.time()
    with store.connect() as db:
        got = db.one("INSERT INTO searches (day, at, info) VALUES (?, ?, ?) "
                     "ON CONFLICT (day) DO NOTHING RETURNING day",
                     (day, now, json.dumps(info)))
        if got is not None:
            return day, None
        record = _record(db.one("SELECT day, at, info FROM searches WHERE day = ?", (day,)))
        if record is not None and _abandoned(record, now):
            # Take over the unfinished record; the old start time makes this atomic.
            got = db.one("UPDATE searches SET at = ?, info = ? WHERE day = ? AND at = ? RETURNING day",
                         (now, json.dumps(info), day, record["at"]))
            if got is not None:
                return day, None
            record = _record(db.one("SELECT day, at, info FROM searches WHERE day = ?", (day,)))
        return None, record


def finish(day: str, info: dict[str, Any]) -> None:
    """Add what the search found to the day's record."""
    with store.connect() as db:
        row = db.one("SELECT info FROM searches WHERE day = ?", (day,))
        merged = {**(json.loads(row[0]) if row else {}), **info}
        db.run("UPDATE searches SET info = ? WHERE day = ?", (json.dumps(merged), day))


def release(day: str, reason: str | None = None, extra: dict[str, Any] | None = None) -> None:
    """Give the day back after a search that failed (outright, or a source of it).
    With a reason (plain words for the page) the attempt stays in the history,
    marked failed; extra adds what an incomplete search found."""
    with store.connect() as db:
        row = db.one("SELECT at, info FROM searches WHERE day = ?", (day,))
        db.run("DELETE FROM searches WHERE day = ?", (day,))
        if reason and row:
            info = {**json.loads(row[1]), **(extra or {}), "failed": True, "reason": reason}
            db.run("INSERT INTO search_failures (id, day, at, info) VALUES (?, ?, ?, ?)",
                   (uuid.uuid4().hex, day, row[0], json.dumps(info)))


def _abandoned(record: Record | None, now: float) -> bool:
    return record is not None and "leads" not in record and now - record["at"] > STALE_SECONDS


def _record(row: store.Row | None) -> Record | None:
    return _as_record(row) if row else None


def _as_record(row: store.Row) -> Record:
    day, at, info = row
    return {"day": day, "at": at, "when": date_time_text(at), **json.loads(info)}


def history(limit: int = 60) -> dict[str, Any]:
    """Recent days' searches (failed attempts too), newest first, and whether
    today's has been used."""
    with store.connect() as db:
        rows = db.all("SELECT day, at, info FROM searches ORDER BY day DESC LIMIT ?", (limit,))
        failed = db.all("SELECT day, at, info FROM search_failures ORDER BY at DESC LIMIT ?",
                        (limit,))
    records = [_as_record(r) for r in rows]
    current = records[0] if records and records[0]["day"] == today() else None
    searches = sorted(records + [_as_record(r) for r in failed], key=lambda r: -r["at"])[:limit]
    return {"today": today(), "used_today": bool(current) and not _abandoned(current, time.time()),
            "current": current, "searches": searches}
