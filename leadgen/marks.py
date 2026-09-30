"""Whether a business has a baler, as marked on the results page ("yes" / "no").

Marks belong to a saved lead (its uid, see saved.py), so they stay with the
business on later searches, and live in the database (store.py). Every click
is also logged with the mark it replaced (mark_changes), so a misclick can be
undone for UNDO_SECONDS, exactly and only while no newer click has followed.
"""

import time
import uuid
from collections.abc import Iterable
from typing import Any

from . import store
from .localtime import date_time_text
from .models import Lead, Undo

VALUES = ("yes", "no")
UNDO_SECONDS = 5 * 60           # how long after a click it can still be undone


def get_all(uids: Iterable[str]) -> dict[str, str]:
    """{uid: "yes"/"no"} for the given uids that have a mark.

    Only the marks of those businesses are read (the whole table only when most
    of it is wanted anyway). Raises store.Unavailable or a database error when
    the marks can't be read: an unreadable mark must never look like "Not checked".
    """
    wanted_list = [u for u in dict.fromkeys(uids) if u]
    if not wanted_list:
        return {}
    with store.connect() as db:
        rows = store.rows_for(db, "SELECT uid, value FROM marks", "uid", wanted_list)
    wanted = set(wanted_list)
    return {uid: value for uid, value in rows if uid in wanted and value in VALUES}


def changed_since(ts: float) -> set[str]:
    """The uids whose mark was set, switched or undone after ts (epoch seconds)."""
    with store.connect() as db:
        rows = db.all("SELECT uid FROM mark_changes WHERE at > ? OR undone_at > ?", (ts, ts))
    return {uid for (uid,) in rows}


def _current(db: store.Db, uid: str) -> str:
    lock = " FOR UPDATE" if db.postgres else ""
    row = db.one("SELECT value FROM marks WHERE uid = ?" + lock, (uid,))
    return row[0] if row else ""


def set_mark(uid: str, value: str, by: str = "") -> Undo | None:
    """Save a mark ("yes" or "no"; a mark is only taken back by undo()) made by `by`
    (a name, or "" when unknown). Returns the undo for the click ({"id", "until"}),
    or None when nothing changed.

    Raises ValueError for a bad value or a business that isn't saved, and
    store.Unavailable or a database error when it can't be saved.
    """
    if value not in VALUES:
        raise ValueError("A baler mark must be yes or no")
    now = time.time()
    with store.connect() as db, db.transaction():
        if db.one("SELECT uid FROM leads WHERE uid = ?", (uid,)) is None:
            raise ValueError("Unknown lead")
        previous = _current(db, uid)
        if previous == value:
            return None
        db.run("INSERT INTO marks (uid, value, updated_at) VALUES (?, ?, ?) "
               "ON CONFLICT (uid) DO UPDATE SET value = excluded.value, "
               "updated_at = excluded.updated_at", (uid, value, now))
        change = uuid.uuid4().hex
        db.run("INSERT INTO mark_changes (id, uid, value, previous, at) VALUES (?, ?, ?, ?, ?)",
               (change, uid, value, previous, now))
        store.record_maker(db, change, store.person(by))
    return {"id": change, "until": now + UNDO_SECONDS}


def undo(change_id: str) -> bool:
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


def pending_undos() -> dict[str, Undo]:
    """{uid: {"id", "until"}}: each business's latest click that can still be undone."""
    now = time.time()
    with store.connect() as db:
        rows = db.all("SELECT id, uid, at FROM mark_changes WHERE at > ? AND undone_at IS NULL "
                      "ORDER BY at", (now - UNDO_SECONDS,))
    return {uid: {"id": change, "until": at + UNDO_SECONDS} for change, uid, at in rows}


def _clicks(uids: Iterable[str]) -> list[tuple[str, str, float | None]]:
    """(uid, who, undone_at) of every Yes / No click on these businesses, oldest first."""
    wanted = [u for u in dict.fromkeys(uids) if u]
    if not wanted:
        return []
    with store.connect() as db:
        return [(uid, name or "", undone_at) for uid, name, undone_at in store.rows_for(
            db, "SELECT mark_changes.uid, made_by.name, mark_changes.undone_at FROM mark_changes "
                "LEFT JOIN made_by ON made_by.id = mark_changes.id", "mark_changes.uid", wanted,
            order="ORDER BY mark_changes.at")]


def history(uid: str) -> list[dict[str, Any]]:
    """Every Yes / No click on a business that was not undone, newest first (with who
    made it): what it was marked before, including the marks of listings merged into it."""
    with store.connect() as db:
        rows = db.all("SELECT mark_changes.at, mark_changes.value, made_by.name FROM mark_changes "
                      "LEFT JOIN made_by ON made_by.id = mark_changes.id "
                      "WHERE mark_changes.uid = ? AND mark_changes.undone_at IS NULL "
                      "ORDER BY mark_changes.at DESC", (uid,))
    return [{"at": at, "when": date_time_text(at), "value": value, "by": by or ""}
            for at, value, by in rows]


def apply(leads: list[Lead]) -> list[Lead]:
    """Set lead.has_baler (who marked it, and how many clicks its mark history holds)
    from the saved marks (raises when they can't be read)."""
    found = get_all(lead.uid for lead in leads)
    clicks = _clicks(lead.uid for lead in leads if lead.uid in found)
    latest: dict[str, str] = {}
    kept: dict[str, int] = {}
    for uid, name, undone_at in clicks:
        if undone_at is None:
            latest[uid] = name
            kept[uid] = kept.get(uid, 0) + 1
    for lead in leads:
        lead.has_baler = found.get(lead.uid, "")
        lead.marked_by = latest.get(lead.uid, "") if lead.has_baler else ""
        lead.mark_clicks = kept.get(lead.uid, 0) if lead.has_baler else 0
    return leads
