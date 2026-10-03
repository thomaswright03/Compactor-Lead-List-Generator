"""Verified contact details: a phone number and who to ask for, found by a salesperson
while checking a business (a direct line, the facilities manager's name), saved on the
lead for the whole team.

They live in a table of their own (lead_contacts), apart from the listing data, so a
later search that updates the business never touches them, and the phone its listing
gave stays as it was beside them. Every save is a new row with who saved it and when
(nothing here changes or deletes a row): the latest row is the lead's verified contact,
the earlier ones its history. Saving both boxes empty is a row too, and takes the
verified contact off the lead.
"""

import re
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from . import store
from .export import format_phone
from .localtime import date_time_text
from .models import Lead

MAX_PHONE = 40
MAX_CONTACT = 80

BAD_PHONE = "Type the phone number with its area code, for example (801) 555-0199."
NOTHING = "Type a verified phone number, who to ask for, or both."
NO_NAME = "Enter your name first, so the team can see who saved this."


@dataclass(frozen=True)
class Contact:
    """A lead's verified phone and contact name ("" when not given), who saved them and when."""

    phone: str
    name: str
    by: str
    at: float

    def as_json(self, saves: int = 0) -> dict[str, Any]:
        """As the page gets it, with the same keys as the lead's own (web/common.py lead_json);
        saves: how many times the lead's verified contact has been saved."""
        empty = not (self.phone or self.name)
        return {"verified_phone": format_phone(self.phone), "contact_name": self.name,
                "contact_by": "" if empty else self.by,
                "contact_when": "" if empty else date_time_text(self.at),
                "contact_at": None if empty else self.at, "contact_saves": saves}


def saves(uid: str) -> int:
    """How many times a lead's verified contact was saved."""
    return _read([uid])[1].get(uid, 0)


def _line(text: object, limit: int) -> str:
    """One line of printable text, at most `limit` characters."""
    return " ".join("".join(c for c in str(text or "") if c.isprintable()).split())[:limit]


def clean_phone(text: object) -> str:
    """The phone as typed (one line), or "" for none. Raises ValueError unless it has an
    area code: 10 digits, or 11 starting with 1, before any extension ("ext. 12")."""
    phone = _line(text, MAX_PHONE)
    if not phone:
        return ""
    number = re.split(r"(?:ext\.?|extension|x|#)\s*\d", phone, maxsplit=1, flags=re.IGNORECASE)[0]
    digits = re.sub(r"\D", "", number)
    if not (len(digits) == 10 or (len(digits) == 11 and digits.startswith("1"))):
        raise ValueError(BAD_PHONE)
    return phone


def _latest(db: store.Db, uid: str) -> Contact | None:
    row = db.one("SELECT phone, contact, by_name, at FROM lead_contacts WHERE uid = ? "
                 "ORDER BY at DESC LIMIT 1", (uid,))
    return Contact(*row) if row else None


def save(uid: str, phone: str, name: str, by: str) -> Contact:
    """Save a lead's verified phone and contact name, as `by` typed them; returns what
    the lead now has. The same details again change nothing. Raises ValueError for an
    unknown lead, no name, a phone without an area code, or nothing typed on a lead
    that has nothing to take off."""
    by = store.person(by)
    if not by:
        raise ValueError(NO_NAME)
    phone, name = clean_phone(phone), _line(name, MAX_CONTACT)
    with store.connect() as db, db.transaction():
        if db.one("SELECT uid FROM leads WHERE uid = ?", (uid,)) is None:
            raise ValueError("That business isn't in the saved list. Reload the page.")
        now = _latest(db, uid)
        if now and (now.phone, now.name) == (phone, name):
            return now
        if not (phone or name) and not (now and (now.phone or now.name)):
            raise ValueError(NOTHING)
        contact = Contact(phone, name, by, time.time())
        db.run("INSERT INTO lead_contacts (id, uid, phone, contact, by_name, at) VALUES (?, ?, ?, ?, ?, ?)",
               (uuid.uuid4().hex, uid, phone, name, by, contact.at))
    return contact


def _read(uids: Iterable[str]) -> tuple[dict[str, Contact], dict[str, int]]:
    """({uid: its latest save}, {uid: how many saves}) for the given leads that have any."""
    wanted = [u for u in dict.fromkeys(uids) if u]
    if not wanted:
        return {}, {}
    with store.connect() as db:
        rows = store.rows_for(db, "SELECT uid, phone, contact, by_name, at FROM lead_contacts", "uid",
                              wanted, order="ORDER BY at")
    keep = set(wanted)
    found: dict[str, Contact] = {}
    saves: dict[str, int] = {}
    for uid, phone, name, by, at in rows:
        if uid in keep:
            found[uid] = Contact(phone, name, by, at)
            saves[uid] = saves.get(uid, 0) + 1
    return found, saves


def latest(uids: Iterable[str]) -> dict[str, Contact]:
    """{uid: its verified contact} for the given leads that have one."""
    found, _ = _read(uids)
    return {uid: c for uid, c in found.items() if c.phone or c.name}


def apply(leads: list[Lead]) -> list[Lead]:
    """Set each lead's verified phone and contact name, who saved them when, and how many
    times its verified contact was saved (its history).

    Raises store.Unavailable or a database error when they can't be read."""
    found, saves = _read(lead.uid for lead in leads)
    for lead in leads:
        c = found.get(lead.uid)
        if c and not (c.phone or c.name):
            c = None                                  # taken off
        lead.verified_phone, lead.contact_name = (c.phone, c.name) if c else ("", "")
        lead.contact_by, lead.contact_at = (c.by, c.at) if c else ("", None)
        lead.contact_saves = saves.get(lead.uid, 0)
    return leads


def changed_since(ts: float) -> set[str]:
    """The uids whose verified contact was saved after ts (epoch seconds)."""
    with store.connect() as db:
        rows = db.all("SELECT uid FROM lead_contacts WHERE at > ?", (ts,))
    return {uid for (uid,) in rows}


def history(uid: str) -> list[dict[str, Any]]:
    """Every verified contact saved on a lead, newest first."""
    with store.connect() as db:
        rows = db.all("SELECT phone, contact, by_name, at FROM lead_contacts WHERE uid = ? "
                      "ORDER BY at DESC", (uid,))
    return [{"phone": format_phone(phone), "contact": name, "by": by, "when": date_time_text(at)}
            for phone, name, by, at in rows]
