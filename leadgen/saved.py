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
from dataclasses import asdict, fields

from . import config, store
from .dedupe import _merge, is_duplicate, snapshot
from .geo import haversine_miles
from .models import Lead
from .scoring import score_lead

_FIELDS = {f.name for f in fields(Lead)}


def _to_lead(data):
    return Lead(**{k: v for k, v in data.items() if k in _FIELDS})


def _lead_json(lead):
    return json.dumps({k: v for k, v in asdict(lead).items()
                       if k not in ("parts", "has_baler", "last_call_at", "call_outcome", "call_notes",
                                    "call_count")}, separators=(",", ":"))


def _part_id(part):
    lead = part["lead"]
    return f"{lead.get('source')}:{lead.get('source_id')}"


def _expired(part, now):
    keep = config.SAVED_SOURCE_KEEP_SECONDS.get(part["lead"].get("source"))
    return keep is not None and now - part["at"] > keep


def _rebuild(parts):
    lead = _merge([_to_lead(copy.deepcopy(p["lead"])) for p in parts])
    score_lead(lead, config.DEFAULT_KEYWORDS)
    return lead


def _bucket(lead):
    return (round(lead.lat, 2), round(lead.lon, 2))


class _Row:
    def __init__(self, uid, lead, parts, ids, first_seen, last_seen):
        self.uid, self.lead, self.parts, self.ids = uid, lead, parts, ids
        self.first_seen, self.last_seen = first_seen, last_seen

    def values(self):
        return (self.uid, _lead_json(self.lead) if self.lead else "null",
                json.dumps(self.parts, separators=(",", ":")), json.dumps(sorted(self.ids)),
                self.first_seen, self.last_seen)


def _read(db, uids=None):
    """Every saved row, or (with uids) just those rows."""
    select = "SELECT uid, lead, parts, ids, first_seen, last_seen FROM leads"
    found = db.all(select) if uids is None else store.rows_for(db, select, "uid", uids)
    rows = []
    for uid, lead, parts, ids, first, last in found:
        data = json.loads(lead)
        rows.append(_Row(uid, _to_lead(data) if data else None, json.loads(parts),
                         set(json.loads(ids)), first, last))
    return rows


def _write(db, rows):
    db.many("INSERT INTO leads (uid, lead, parts, ids, first_seen, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (uid) DO UPDATE SET lead = excluded.lead, "
            "parts = excluded.parts, ids = excluded.ids, last_seen = excluded.last_seen",
            [r.values() for r in rows])


def _drop_expired(rows, now):
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


def save_search(leads, keywords=None):
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
        buckets: dict[tuple, list] = {}
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
                        if r.uid not in claimed and is_duplicate(lead, r.lead)]
                row = min(near, key=lambda r: haversine_miles(lead.lat, lead.lon, r.lead.lat,
                                                              r.lead.lon), default=None)
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


def load(uids=None):
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
            rows = [_Row(uid, _to_lead(data) if (data := json.loads(lead)) else None, None,
                         None, None, None)
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


def changed_since(ts):
    """The uids of saved leads that a search added or updated after ts (epoch seconds)."""
    with store.connect() as db:
        return {uid for (uid,) in db.all("SELECT uid FROM leads WHERE last_seen > ?", (ts,))}


def date_range():
    """(first, latest): when the first and the latest search that saved leads ran
    (epoch seconds), or (None, None) for an empty list."""
    with store.connect() as db:
        first, latest = db.one("SELECT MIN(first_seen), MAX(last_seen) FROM leads")
    return first, latest
