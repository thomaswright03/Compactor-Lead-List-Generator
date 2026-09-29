"""The call log: every "Just called" note, kept for good (nothing here deletes).

A call belongs to a saved lead (its uid). Its outcome is one of OUTCOMES; the
latest call's outcome is the lead's current one (the page's call tabs).
"""

import time
import uuid

from . import store
from .localtime import date_time_text

OUTCOMES = ("Interested", "Follow Up", "Not Interested", "Not Qualified", "No Contact",
            "Bad Lead")
MAX_NOTES = 5000


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


UNDO_SECONDS = 5 * 60           # how long after saving a call it can still be undone


def undo(call_id):
    """Delete a call saved in the last UNDO_SECONDS. False when it is too late (or unknown)."""
    now = time.time()
    with store.connect() as db, db.transaction():
        row = db.one("DELETE FROM calls WHERE id = ? AND at > ? RETURNING uid",
                     (call_id, now - UNDO_SECONDS))
        if row is not None:
            db.run("INSERT INTO call_undos (id, uid, at) VALUES (?, ?, ?)", (call_id, row[0], now))
    return row is not None


def changed_since(ts):
    """The uids with a call saved or undone after ts (epoch seconds)."""
    with store.connect() as db:
        rows = db.all("SELECT uid FROM calls WHERE at > ? UNION SELECT uid FROM call_undos "
                      "WHERE at > ?", (ts, ts))
    return {uid for (uid,) in rows}


def history(uid):
    """Every call to a lead, newest first."""
    with store.connect() as db:
        rows = db.all("SELECT at, outcome, notes FROM calls WHERE uid = ? ORDER BY at DESC",
                      (uid,))
    return [{"at": at, "when": date_time_text(at), "outcome": outcome, "notes": notes}
            for at, outcome, notes in rows]


def pending_undos():
    """{uid: {"id", "until"}}: each business's latest call that can still be undone."""
    now = time.time()
    with store.connect() as db:
        rows = db.all("SELECT id, uid, at FROM calls WHERE at > ? ORDER BY at",
                      (now - UNDO_SECONDS,))
    return {uid: {"id": call_id, "until": at + UNDO_SECONDS} for call_id, uid, at in rows}


def apply(leads):
    """Set each lead's latest call and call count.

    Raises store.Unavailable or a database error when the calls can't be read:
    an unreadable call log must never look like "never called".
    """
    with store.connect() as db:
        rows = store.rows_for(db, "SELECT uid, at, outcome, notes FROM calls", "uid",
                              dict.fromkeys(lead.uid for lead in leads if lead.uid),
                              order="ORDER BY at")
    latest, counts = {}, {}
    for uid, at, outcome, notes in rows:
        latest[uid] = (at, outcome, notes)
        counts[uid] = counts.get(uid, 0) + 1
    for lead in leads:
        at, outcome, notes = latest.get(lead.uid, (None, "", ""))
        lead.last_call_at, lead.call_outcome, lead.call_notes = at, outcome, notes
        lead.call_count = counts.get(lead.uid, 0)
    return leads
