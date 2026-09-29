"""Whether a business has a baler, as marked on the results page ("yes" / "no").

Marks belong to a saved lead (its uid, see saved.py), so they stay with the
business on later searches, and live in the database (store.py). Every click
is also logged with the mark it replaced (mark_changes), so a misclick can be
undone for UNDO_SECONDS, exactly and only while no newer click has followed.
"""

import time
import uuid

from . import store

VALUES = ("yes", "no")
UNDO_SECONDS = 5 * 60           # how long after a click it can still be undone


def get_all(uids):
    """{uid: "yes"/"no"} for the given uids that have a mark.

    Raises store.Unavailable or a database error when the marks can't be read:
    an unreadable mark must never look like "Not checked".
    """
    uids = [u for u in dict.fromkeys(uids) if u]
    if not uids:
        return {}
    with store.connect() as db:
        rows = db.all("SELECT uid, value FROM marks")
    wanted = set(uids)
    return {uid: value for uid, value in rows if uid in wanted and value in VALUES}


def _current(db, uid):
    lock = " FOR UPDATE" if db.postgres else ""
    row = db.one("SELECT value FROM marks WHERE uid = ?" + lock, (uid,))
    return row[0] if row else ""


def set_mark(uid, value):
    """Save a mark; an empty value clears it. Returns the undo for the click
    ({"id", "until"}), or None when nothing changed.

    Raises ValueError for a bad value or a business that isn't saved, and
    store.Unavailable or a database error when it can't be saved.
    """
    if value not in (*VALUES, ""):
        raise ValueError("A baler mark must be yes, no or empty")
    now = time.time()
    with store.connect() as db, db.transaction():
        if db.one("SELECT uid FROM leads WHERE uid = ?", (uid,)) is None:
            raise ValueError("Unknown lead")
        previous = _current(db, uid)
        if previous == value:
            return None
        if value:
            db.run("INSERT INTO marks (uid, value, updated_at) VALUES (?, ?, ?) "
                   "ON CONFLICT (uid) DO UPDATE SET value = excluded.value, "
                   "updated_at = excluded.updated_at", (uid, value, now))
        else:
            db.run("DELETE FROM marks WHERE uid = ?", (uid,))
        change = uuid.uuid4().hex
        db.run("INSERT INTO mark_changes (id, uid, value, previous, at) VALUES (?, ?, ?, ?, ?)",
               (change, uid, value, previous, now))
    return {"id": change, "until": now + UNDO_SECONDS}


def undo(change_id):
    """Put back the mark a recent click replaced. False when it is too late (older
    than UNDO_SECONDS), already undone, unknown, or a newer click has followed it."""
    now = time.time()
    with store.connect() as db, db.transaction():
        row = db.one("SELECT uid, value, previous, at, undone_at FROM mark_changes WHERE id = ?",
                     (change_id,))
        if row is None:
            return False
        uid, value, previous, at, undone_at = row
        if undone_at is not None or at <= now - UNDO_SECONDS:
            return False
        if _current(db, uid) != value:
            return False
        latest = db.one("SELECT id FROM mark_changes WHERE uid = ? AND undone_at IS NULL "
                        "ORDER BY at DESC LIMIT 1", (uid,))
        if latest is None or latest[0] != change_id:
            return False
        if previous:
            db.run("UPDATE marks SET value = ?, updated_at = ? WHERE uid = ?",
                   (previous, now, uid))
        else:
            db.run("DELETE FROM marks WHERE uid = ?", (uid,))
        db.run("UPDATE mark_changes SET undone_at = ? WHERE id = ?", (now, change_id))
    return True


def pending_undos():
    """{uid: {"id", "until"}}: each business's latest click that can still be undone."""
    now = time.time()
    with store.connect() as db:
        rows = db.all("SELECT id, uid, at FROM mark_changes WHERE at > ? AND undone_at IS NULL "
                      "ORDER BY at", (now - UNDO_SECONDS,))
    return {uid: {"id": change, "until": at + UNDO_SECONDS} for change, uid, at in rows}


def apply(leads):
    """Set lead.has_baler from the saved marks (raises when they can't be read)."""
    found = get_all(lead.uid for lead in leads)
    for lead in leads:
        lead.has_baler = found.get(lead.uid, "")
    return leads
