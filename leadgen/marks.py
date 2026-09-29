"""Whether a business has a baler, as marked on the results page ("yes" / "no").

Marks belong to a saved lead (its uid, see saved.py), so they stay with the
business on later searches, and live in the database (store.py).
"""

import time

from . import store

VALUES = ("yes", "no")
UNDO_SECONDS = 5 * 60           # how long after a click it can still be undone


def get_all(uids):
    """{uid: "yes"/"no"} for the given uids that have a mark; {} when unreadable."""
    uids = [u for u in dict.fromkeys(uids) if u]
    if not uids:
        return {}
    try:
        with store.connect() as db:
            rows = db.all("SELECT uid, value FROM marks")
    except Exception:
        return {}
    wanted = set(uids)
    return {uid: value for uid, value in rows if uid in wanted and value in VALUES}


def set_mark(uid, value):
    """Save a mark; an empty value clears it. Raises store.Unavailable or a database error."""
    if value not in VALUES + ("",):
        raise ValueError("A baler mark must be yes, no or empty")
    with store.connect() as db:
        if value:
            db.run("INSERT INTO marks (uid, value, updated_at) VALUES (?, ?, ?) "
                   "ON CONFLICT (uid) DO UPDATE SET value = excluded.value, "
                   "updated_at = excluded.updated_at", (uid, value, time.time()))
        else:
            db.run("DELETE FROM marks WHERE uid = ?", (uid,))


def undo(uid, previous):
    """Put back the mark a recent click replaced ("" = Not checked). False when the
    mark was not changed in the last UNDO_SECONDS (too late to undo)."""
    if previous not in VALUES + ("",):
        raise ValueError("A baler mark must be yes, no or empty")
    recent = time.time() - UNDO_SECONDS
    with store.connect() as db:
        if previous:
            row = db.one("UPDATE marks SET value = ? WHERE uid = ? AND updated_at > ? "
                         "RETURNING uid", (previous, uid, recent))
        else:
            row = db.one("DELETE FROM marks WHERE uid = ? AND updated_at > ? RETURNING uid",
                         (uid, recent))
    return row is not None


def apply(leads):
    """Set lead.has_baler from the saved marks."""
    found = get_all(l.uid for l in leads)
    for lead in leads:
        lead.has_baler = found.get(lead.uid, "")
    return leads
