"""The call log: every "Just called" note, kept for good (nothing here deletes).

A call belongs to a saved lead (its uid). Its outcome is one of OUTCOMES; the
latest call's outcome is the lead's current one (the page's call tabs).
"""

import datetime as dt
import time
import uuid

from . import store

OUTCOMES = ("Interested", "Follow Up", "Not Interested", "Not Qualified", "No Contact",
            "Bad Lead")
MAX_NOTES = 5000


def local_time_text(ts):
    """'Sep 29, 2026, 10:14 am' in Utah time."""
    if not ts:
        return ""
    when = dt.datetime.fromtimestamp(ts, dt.timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        when = when.astimezone(ZoneInfo("America/Denver"))
    except Exception:
        pass
    hour = when.hour % 12 or 12
    return f"{when:%b} {when.day}, {when.year}, {hour}:{when:%M} {'am' if when.hour < 12 else 'pm'}"


def log_call(uid, outcome, notes):
    """Record a call; returns it. Raises ValueError for a bad outcome or unknown lead."""
    if outcome not in OUTCOMES:
        raise ValueError("Pick how the call went")
    notes = (notes or "").strip()[:MAX_NOTES]
    call = {"id": uuid.uuid4().hex, "uid": uid, "at": time.time(), "outcome": outcome,
            "notes": notes}
    with store.connect() as db:
        if db.one("SELECT uid FROM leads WHERE uid = ?", (uid,)) is None:
            raise ValueError("Unknown lead")
        db.run("INSERT INTO calls (id, uid, at, outcome, notes) VALUES (?, ?, ?, ?, ?)",
               (call["id"], uid, call["at"], outcome, notes))
    return call


def history(uid):
    """Every call to a lead, newest first."""
    with store.connect() as db:
        rows = db.all("SELECT at, outcome, notes FROM calls WHERE uid = ? ORDER BY at DESC",
                      (uid,))
    return [{"at": at, "when": local_time_text(at), "outcome": outcome, "notes": notes}
            for at, outcome, notes in rows]


def apply(leads):
    """Set each lead's latest call and call count; leaves them unset when unreadable."""
    try:
        with store.connect() as db:
            rows = db.all("SELECT uid, at, outcome, notes FROM calls ORDER BY at")
    except Exception:
        return leads
    latest, counts = {}, {}
    for uid, at, outcome, notes in rows:
        latest[uid] = (at, outcome, notes)
        counts[uid] = counts.get(uid, 0) + 1
    for lead in leads:
        at, outcome, notes = latest.get(lead.uid, (None, "", ""))
        lead.last_call_at, lead.call_outcome, lead.call_notes = at, outcome, notes
        lead.call_count = counts.get(lead.uid, 0)
    return leads
