"""The saved lead list: every search merges into it, one row per business.

A saved lead keeps the source listings it was built from ("parts"). A new
search's lead joins the saved one it matches (same source listing, or the
duplicate rules in dedupe.py) and replaces that source's listings with the
fresh ones, so a business never gets a second row and keeps its id (and its
baler mark). Listings from Yelp and Google are dropped once their terms stop
allowing them to be kept (config.SAVED_SOURCE_KEEP_SECONDS); the lead is then
rebuilt from what is left, and a lead with nothing left is hidden but keeps
its listing ids, so its mark comes back if a search finds it again.

A saved lead is always scored with the default keywords
(config.DEFAULT_KEYWORDS), never with what someone typed into a later search,
so its score and tier depend only on facts about the business and the Stats
page's tiers stay comparable over time.
"""

import copy
import json
import time
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import asdict, fields
from typing import Any

from . import config, store
from .dedupe import _merge, is_duplicate, snapshot
from .geo import haversine_miles
from .models import Lead
from .scoring import score_lead

_FIELDS = {f.name for f in fields(Lead)}
# One source listing kept with a saved lead: {"at": when it was found, "lead": its fields}.
Part = dict[str, Any]


def _to_lead(data: dict[str, Any]) -> Lead:
    return Lead(**{k: v for k, v in data.items() if k in _FIELDS})


def _lead_json(lead: Lead) -> str:
    return json.dumps({k: v for k, v in asdict(lead).items()
                       if k not in ("parts", "has_baler", "last_call_at", "call_outcome", "call_notes",
                                    "call_count", "earlier_notes", "earlier_notes_at")}, separators=(",", ":"))


def _part_id(part: Part) -> str:
    lead = part["lead"]
    return f"{lead.get('source')}:{lead.get('source_id')}"


def _expired(part: Part, now: float) -> bool:
    keep = config.SAVED_SOURCE_KEEP_SECONDS.get(part["lead"].get("source"))
    return keep is not None and now - part["at"] > keep


def _rebuild(parts: list[Part]) -> Lead:
    lead = _merge([_to_lead(copy.deepcopy(p["lead"])) for p in parts])
    score_lead(lead, config.DEFAULT_KEYWORDS)
    return lead


def _miles_between(lead: Lead, other: Lead | None) -> float:
    return haversine_miles(lead.lat, lead.lon, other.lat, other.lon) if other else float("inf")


def _bucket(lead: Lead) -> tuple[float, float]:
    return (round(lead.lat, 2), round(lead.lon, 2))


class _Row:
    def __init__(self, uid: str, lead: Lead | None, parts: list[Part], ids: set[str],
                 first_seen: float, last_seen: float) -> None:
        self.uid, self.lead, self.parts, self.ids = uid, lead, parts, ids
        self.first_seen, self.last_seen = first_seen, last_seen

    def values(self) -> tuple[str, str, str, str, float, float]:
        return (self.uid, _lead_json(self.lead) if self.lead else "null",
                json.dumps(self.parts, separators=(",", ":")), json.dumps(sorted(self.ids)),
                self.first_seen, self.last_seen)


def _read(db: store.Db, uids: list[str] | None = None) -> list[_Row]:
    """Every saved row, or (with uids) just those rows."""
    select = "SELECT uid, lead, parts, ids, first_seen, last_seen FROM leads"
    found = db.all(select) if uids is None else store.rows_for(db, select, "uid", uids)
    rows = []
    for uid, lead, parts, ids, first, last in found:
        data = json.loads(lead)
        rows.append(_Row(uid, _to_lead(data) if data else None, json.loads(parts),
                         set(json.loads(ids)), first, last))
    return rows


def _write(db: store.Db, rows: Iterable[_Row]) -> None:
    db.many("INSERT INTO leads (uid, lead, parts, ids, first_seen, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (uid) DO UPDATE SET lead = excluded.lead, "
            "parts = excluded.parts, ids = excluded.ids, last_seen = excluded.last_seen",
            [r.values() for r in rows])


def _drop_expired(rows: list[_Row], now: float) -> list[_Row]:
    """Remove listings that may no longer be kept; returns the rows that changed."""
    changed = []
    for row in rows:
        fresh = [p for p in row.parts if not _expired(p, now)]
        if len(fresh) == len(row.parts):
            continue
        row.parts = fresh
        row.lead = _rebuild(fresh) if fresh else None
        if row.lead:
            row.lead.uid = row.uid
        changed.append(row)
    return changed


def save_search(leads: list[Lead], keywords: Sequence[str] | None = None) -> tuple[int, int]:
    """Merge a search's leads into the saved list; sets each lead's uid once saved.

    keywords: what the search was scored with; a lead scored with anything but
    the default keywords is scored again with them before it is saved.

    Leads the search found permanently closed update their row too (it then
    leaves the list). Returns (new, updated) counts. Raises store.Unavailable
    without a database, or a database error; no lead gets a uid then.
    """
    now = time.time()
    rescore = keywords is None or [k.lower() for k in keywords] != [
        k.lower() for k in config.DEFAULT_KEYWORDS]
    with store.connect() as db:
        rows = _read(db)
        changed = {r.uid: r for r in _drop_expired(rows, now)}
        by_id = {i: r for r in rows for i in r.ids}
        buckets: dict[tuple[float, float], list[_Row]] = {}
        for r in rows:
            if r.lead:
                buckets.setdefault(_bucket(r.lead), []).append(r)
        claimed, new, uids = set(), 0, []
        for lead in leads:
            parts = [{"at": now, "lead": p} for p in snapshot(lead)]
            ids = {_part_id(p) for p in parts}
            # Two leads the search kept apart never share a row, even via a listing id.
            row = next((by_id[i] for i in sorted(ids)
                        if i in by_id and by_id[i].uid not in claimed), None)
            if row is None:
                bx, by = _bucket(lead)
                near = [r for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                        for r in buckets.get((round(bx + dx * 0.01, 2), round(by + dy * 0.01, 2)), [])
                        if r.uid not in claimed and r.lead is not None and is_duplicate(lead, r.lead)]
                row = min(near, key=lambda r: _miles_between(lead, r.lead), default=None)
            if row is None:
                row = _Row(uuid.uuid4().hex, None, [], set(), now, now)
                rows.append(row)
                new += lead.business_status != "CLOSED_PERMANENTLY"
            older = [p for p in row.parts if _part_id(p) not in ids]
            row.parts = parts + older
            row.ids |= ids
            row.last_seen = now
            if older:
                row.lead = _rebuild(row.parts)
            else:
                row.lead = copy.deepcopy(lead)
                if rescore:
                    score_lead(row.lead, config.DEFAULT_KEYWORDS)
            if lead.business_status == "CLOSED_PERMANENTLY":
                row.lead.business_status = lead.business_status
            row.lead.uid = row.uid
            row.lead.parts = []
            uids.append(row.uid)
            claimed.add(row.uid)
            changed[row.uid] = row
            for i in row.ids:
                by_id[i] = row
            buckets.setdefault(_bucket(row.lead), []).append(row)
        _write(db, changed.values())
    for lead, uid in zip(leads, uids):
        lead.uid = uid
    shown = sum(l.business_status != "CLOSED_PERMANENTLY" for l in leads)
    return new, shown - new


def load(uids: Iterable[str] | None = None) -> list[Lead]:
    """All saved leads (best first), or with uids just those, with miles from
    Arco's shop. Their marks and calls are added by marks.apply / calls.apply.

    Raises store.Unavailable without a database, or a database error when the
    leads can't be read.
    """
    now = time.time()
    wanted = None if uids is None else set(uids)
    with store.connect() as db:
        if config.SAVED_SOURCE_KEEP_SECONDS:
            rows = _read(db, None if wanted is None else list(wanted))
            _write(db, _drop_expired(rows, now))
        else:
            # Nothing expires, so the source listings (most of each row) needn't be read.
            select = "SELECT uid, lead FROM leads"
            found = (db.all(select) if wanted is None
                     else store.rows_for(db, select, "uid", list(wanted)))
            rows = [_Row(uid, _to_lead(data) if (data := json.loads(lead)) else None, [],
                         set(), 0.0, 0.0)
                    for uid, lead in found]
    leads = []
    for row in rows:
        if wanted is not None and row.uid not in wanted:
            continue
        if row.lead and row.lead.business_status != "CLOSED_PERMANENTLY":
            row.lead.uid = row.uid
            row.lead.distance_miles = round(haversine_miles(
                *config.DEFAULT_CENTER, row.lead.lat, row.lead.lon), 2)
            leads.append(row.lead)
    leads.sort(key=lambda l: (-l.score, l.distance_miles, l.name.lower()))
    return leads


def count() -> int:
    """How many saved leads the list shows (every one not closed for good)."""
    with store.connect() as db:
        rows = db.all("SELECT lead FROM leads")
    return sum(1 for (lead,) in rows
               if (data := json.loads(lead)) and data.get("business_status") != "CLOSED_PERMANENTLY")


def changed_since(ts: float) -> set[str]:
    """The uids of saved leads that a search added or updated after ts (epoch seconds)."""
    with store.connect() as db:
        return {uid for (uid,) in db.all("SELECT uid FROM leads WHERE last_seen > ?", (ts,))}


def date_range() -> tuple[float | None, float | None]:
    """(first, latest): when the first and the latest search that saved leads ran
    (epoch seconds), or (None, None) for an empty list."""
    with store.connect() as db:
        row = db.one("SELECT MIN(first_seen), MAX(last_seen) FROM leads")
    return (row[0], row[1]) if row else (None, None)
