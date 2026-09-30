"""The call log: every "Just called" note, kept for good (nothing here deletes).

A call belongs to a saved lead (its uid). Its outcome is one of OUTCOMES; the
latest call's outcome is the lead's current one (the page's call tabs).
"""

import re
import time
import uuid
from typing import Any

from . import store
from .localtime import date_time_text
from .models import Lead, Undo

OUTCOMES = ("Interested", "Follow Up", "Not Interested", "Not Qualified", "No Contact",
            "Bad Lead")
MAX_NOTES = 5000


def log_call(uid: str, outcome: str, notes: str, call_id: str | None = None,
             by: str = "") -> dict[str, Any]:
    """Record a call made by `by` (a name, or "" when unknown); returns it. Raises
    ValueError for a bad outcome or unknown lead.

    call_id: the page's own id for this call, so a request sent twice (a retry on a
    flaky connection) records one call: the second gets the first one back.
    """
    if outcome not in OUTCOMES:
        raise ValueError("Pick how the call went")
    if call_id is not None and not re.fullmatch(r"[0-9a-f]{32}", call_id):
        raise ValueError("Bad call id")
    notes = (notes or "").strip()[:MAX_NOTES]
    call: dict[str, Any] = {"id": call_id or uuid.uuid4().hex, "uid": uid, "at": time.time(),
                            "outcome": outcome, "notes": notes, "by": store.person(by)}
    with store.connect() as db:
        if db.one("SELECT uid FROM leads WHERE uid = ?", (uid,)) is None:
            raise ValueError("Unknown lead")
        added = db.one("INSERT INTO calls (id, uid, at, outcome, notes) VALUES (?, ?, ?, ?, ?) "
                       "ON CONFLICT (id) DO NOTHING RETURNING id",
                       (call["id"], uid, call["at"], outcome, notes))
        if added is None:                       # sent before: the call saved then
            row = db.one("SELECT uid, at, outcome, notes FROM calls WHERE id = ?", (call["id"],))
            if row is None or row[0] != uid:
                raise ValueError("That call was already undone")
            call.update(at=row[1], outcome=row[2], notes=row[3])
            name = db.one("SELECT name FROM made_by WHERE id = ?", (call["id"],))
            call["by"] = name[0] if name else ""
        elif db.one("SELECT id FROM call_undos WHERE id = ?", (call["id"],)) is not None:
            # A late copy of a call that was saved and then undone: keep it undone.
            db.run("DELETE FROM calls WHERE id = ?", (call["id"],))
            raise ValueError("That call was already undone")
        else:
            store.record_maker(db, call["id"], call["by"])
    return call


UNDO_SECONDS = 5 * 60           # how long after saving a call it can still be undone


def undo(call_id: str) -> bool:
    """Delete a call saved in the last UNDO_SECONDS. False when it is too late (or unknown)."""
    now = time.time()
    with store.connect() as db, db.transaction():
        row = db.one("DELETE FROM calls WHERE id = ? AND at > ? RETURNING uid",
                     (call_id, now - UNDO_SECONDS))
        if row is not None:
            db.run("INSERT INTO call_undos (id, uid, at) VALUES (?, ?, ?)", (call_id, row[0], now))
    return row is not None


def changed_since(ts: float) -> set[str]:
    """The uids with a call saved or undone after ts (epoch seconds)."""
    with store.connect() as db:
        rows = db.all("SELECT uid FROM calls WHERE at > ? UNION SELECT uid FROM call_undos "
                      "WHERE at > ?", (ts, ts))
    return {uid for (uid,) in rows}


def history(uid: str) -> list[dict[str, Any]]:
    """Every call to a lead, newest first."""
    with store.connect() as db:
        rows = db.all("SELECT calls.at, calls.outcome, calls.notes, made_by.name FROM calls "
                      "LEFT JOIN made_by ON made_by.id = calls.id WHERE calls.uid = ? "
                      "ORDER BY calls.at DESC", (uid,))
    return [{"at": at, "when": date_time_text(at), "outcome": outcome, "notes": notes,
             "by": by or ""} for at, outcome, notes, by in rows]


def pending_undos() -> dict[str, Undo]:
    """{uid: {"id", "until"}}: each business's latest call that can still be undone."""
    now = time.time()
    with store.connect() as db:
        rows = db.all("SELECT id, uid, at FROM calls WHERE at > ? ORDER BY at",
                      (now - UNDO_SECONDS,))
    return {uid: {"id": call_id, "until": at + UNDO_SECONDS} for call_id, uid, at in rows}


def apply(leads: list[Lead]) -> list[Lead]:
    """Set each lead's latest call and call count.

    Raises store.Unavailable or a database error when the calls can't be read:
    an unreadable call log must never look like "never called".
    """
    with store.connect() as db:
        rows = store.rows_for(db, "SELECT calls.uid, calls.at, calls.outcome, calls.notes, "
                                  "made_by.name FROM calls LEFT JOIN made_by "
                                  "ON made_by.id = calls.id", "calls.uid",
                              dict.fromkeys(lead.uid for lead in leads if lead.uid),
                              order="ORDER BY calls.at")
    latest: dict[str, tuple[float, str, str]] = {}
    maker: dict[str, str] = {}
    noted: dict[str, tuple[float, str]] = {}              # the latest call with notes
    counts: dict[str, int] = {}
    for uid, at, outcome, notes, by in rows:
        latest[uid] = (at, outcome, notes)
        maker[uid] = by or ""
        if notes:
            noted[uid] = (at, notes)
        counts[uid] = counts.get(uid, 0) + 1
    for lead in leads:
        at, outcome, notes = latest.get(lead.uid, (None, "", ""))
        lead.last_call_at, lead.call_outcome, lead.call_notes = at, outcome, notes
        lead.call_count = counts.get(lead.uid, 0)
        lead.last_call_by = maker.get(lead.uid, "")
        earlier_at, earlier = noted.get(lead.uid, (None, ""))
        if notes or not earlier:
            earlier_at, earlier = None, ""
        lead.earlier_notes, lead.earlier_notes_at = earlier, earlier_at
    return leads
