"""One search a day: Find Leads works once per calendar day, Utah time.

Each day's search is recorded (when it was started, what was searched and what
it found) in the database; the record is claimed atomically when the button is
clicked, so two clicks can never both run. A search that fails before finding
anything (e.g. an unknown location) gives the day back. One where a source failed
(e.g. Google refused its key) while others found businesses is incomplete: what
was found is saved and it uses up the day like a complete search, as the owner
asked (one search per Utah calendar day). INCOMPLETE_RERUNS can allow that many
same-day re-runs after an incomplete search, but it is 0 unless the owner asks. A
search cut off by a server restart saves what it had found and gives the day back
(interrupted.py).
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


# A search takes minutes. One cut off by a server restart is finished, and gives the
# day back, as soon as a page notices (interrupted.py). One that never finished and
# left no record of its run for that (it could not be written) stops holding the
# day after this long, so the day's search can be run again.
STALE_SECONDS = 30 * 60

# After an incomplete search the day's search can be run this many more times. The owner
# asked for one search per Utah day, full stop, so this is 0 (an incomplete search
# uses up the day); raise it only if the owner asks for re-runs.
INCOMPLETE_RERUNS = 0

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


def info(day: str) -> dict[str, Any] | None:
    """What the day's record holds (what was searched and found), or None."""
    with store.connect() as db:
        row = db.one("SELECT info FROM searches WHERE day = ?", (day,))
    return json.loads(row[0]) if row else None


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


def search_times() -> tuple[float | None, float | None]:
    """(first, latest): when the first and the latest search that found leads started
    (epoch seconds, the search history's "When"), or (None, None) before any."""
    with store.connect() as db:
        rows = db.all("SELECT at, info FROM searches")
    started = [at for at, info in rows if "leads" in json.loads(info)]
    return (min(started), max(started)) if started else (None, None)


def earlier_search(place: str, day: str, since: float) -> Record | None:
    """The latest search before `day` of the same place (its looked-up label) that found
    leads and started after `since` (epoch seconds), or None."""
    with store.connect() as db:
        rows = db.all("SELECT day, at, info FROM searches WHERE day < ? AND at > ? ORDER BY at DESC",
                      (day, since))
    for row in rows:
        record = _as_record(row)
        if "leads" in record and place and record.get("place") == place:
            return record
    return None


def incomplete_count(day: str) -> int:
    """How many of the day's searches were incomplete and gave the day back."""
    with store.connect() as db:
        rows = db.all("SELECT info FROM search_failures WHERE day = ?", (day,))
    return sum(1 for (info,) in rows if json.loads(info).get("partial"))


def _abandoned(record: Record | None, now: float) -> bool:
    return record is not None and "leads" not in record and now - record["at"] > STALE_SECONDS


def _record(row: store.Row | None) -> Record | None:
    return _as_record(row) if row else None


# A day whose map areas are still being filled in past this long after the fill-in's
# end was cut short (the server restarted): it reads as interrupted.
FILL_GRACE_SECONDS = 10 * 60


def _as_record(row: store.Row) -> Record:
    day, at, info = row
    record = {"day": day, "at": at, "when": date_time_text(at), **json.loads(info)}
    fill = record.get("fill")
    if isinstance(fill, dict) and fill.get("until"):
        fill["until_text"] = localtime.clock_text(fill["until"])
        if fill.get("state") == "filling" and time.time() > fill["until"] + FILL_GRACE_SECONDS:
            fill["state"] = "interrupted"
    return record


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
    day = today()
    partial = sum(1 for r in failed if r[0] == day and json.loads(r[2]).get("partial"))
    # A search whose missed map areas are still being filled in, whatever its day (one
    # started late in the evening goes on past midnight): the page keeps showing it.
    filling = next((r for r in records if isinstance(r.get("fill"), dict)
                    and r["fill"].get("state") == "filling"), None)
    return {"today": day, "used_today": bool(current) and not _abandoned(current, time.time()),
            "current": current, "searches": searches, "filling": filling,
            "reruns_left": max(0, INCOMPLETE_RERUNS - partial + 1) if partial else None}
