"""One search a day: Find Leads works once per calendar day, Utah time.

Each day's search is recorded (when it was started, what was searched and what
it found) in the database; the record is claimed atomically when the button is
clicked, so two clicks can never both run. A search that fails before finding
anything (e.g. an unknown location) gives the day back.
"""

import datetime as dt
import json
import time

from . import store
from .calls import local_time_text


def _local_now():
    now = dt.datetime.now(dt.timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        return now.astimezone(ZoneInfo("America/Denver"))
    except Exception:
        return now - dt.timedelta(hours=7)


def today():
    return _local_now().strftime("%Y-%m-%d")


# A search takes minutes. One that never finished (the server restarted mid-search)
# stops holding the day after this long, so the day's search can be run again.
STALE_SECONDS = 30 * 60


def claim(info):
    """Record today's search. Returns (day, None), or (None, today's record) if one ran."""
    day, now = today(), time.time()
    with store.connect() as db:
        got = db.one("INSERT INTO searches (day, at, info) VALUES (?, ?, ?) "
                     "ON CONFLICT (day) DO NOTHING RETURNING day",
                     (day, now, json.dumps(info)))
        if got is not None:
            return day, None
        record = _record(db.one("SELECT day, at, info FROM searches WHERE day = ?", (day,)))
        if _abandoned(record, now):
            # Take over the unfinished record; the old start time makes this atomic.
            got = db.one("UPDATE searches SET at = ?, info = ? WHERE day = ? AND at = ? RETURNING day",
                         (now, json.dumps(info), day, record["at"]))
            if got is not None:
                return day, None
            record = _record(db.one("SELECT day, at, info FROM searches WHERE day = ?", (day,)))
        return None, record


def finish(day, info):
    """Add what the search found to the day's record."""
    with store.connect() as db:
        row = db.one("SELECT info FROM searches WHERE day = ?", (day,))
        merged = {**(json.loads(row[0]) if row else {}), **info}
        db.run("UPDATE searches SET info = ? WHERE day = ?", (json.dumps(merged), day))


def release(day):
    with store.connect() as db:
        db.run("DELETE FROM searches WHERE day = ?", (day,))


def _abandoned(record, now):
    return bool(record) and "leads" not in record and now - record["at"] > STALE_SECONDS


def _record(row):
    if not row:
        return None
    day, at, info = row
    return {"day": day, "at": at, "when": local_time_text(at), **json.loads(info)}


def history(limit=60):
    """Recent days' searches, newest first, and whether today's has been used."""
    with store.connect() as db:
        rows = db.all("SELECT day, at, info FROM searches ORDER BY day DESC LIMIT ?", (limit,))
    records = [_record(r) for r in rows]
    current = records[0] if records and records[0]["day"] == today() else None
    return {"today": today(), "used_today": bool(current) and not _abandoned(current, time.time()),
            "searches": records}
