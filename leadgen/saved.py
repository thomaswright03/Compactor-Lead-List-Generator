"""The saved lead list: every search merges into it, one row per business.

A saved lead keeps the source listings it was built from ("parts"). A new
search's lead joins the saved one it matches (same source listing, or the
duplicate rules in dedupe.py) and replaces that source's listings with the
fresh ones, so a business never gets a second row and keeps its id (and its
baler mark). Listings from Yelp and Google are dropped once their terms stop
allowing them to be kept (config.SAVED_SOURCE_KEEP_SECONDS); the lead is then
rebuilt from what is left, and a lead with nothing left is hidden but keeps
its listing ids, so its mark comes back if a search finds it again.

A business a later search reports closed for good stays in the list (with its
mark and calls), flagged CLOSED_FLAG; it is not offered for checking any more.
A closed business that was never saved is not added: nobody prospected it.

A saved lead is always scored with the default keywords
(config.DEFAULT_KEYWORDS), never with what someone typed into a later search,
so its score and tier depend only on facts about the business and the Stats
page's tiers stay comparable over time.
"""

import copy
import json
import threading
import time
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import asdict, fields
from typing import Any

from . import config, store
from .dedupe import is_duplicate, merge, snapshot
from .geo import haversine_miles
from .models import Lead
from .scoring import score_lead

_FIELDS = {f.name for f in fields(Lead)}
CLOSED = "CLOSED_PERMANENTLY"
# The flag a saved business that closed for good carries (on the page and in downloads).
CLOSED_FLAG = "Closed for good"
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
    lead = merge([_to_lead(copy.deepcopy(p["lead"])) for p in parts])
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

    Leads the search found permanently closed update their row if the business
    is already saved (it then stays in the list flagged CLOSED_FLAG, with its
    mark and calls); a closed business that isn't saved yet is not added (and
    gets no uid). Returns (new, updated) counts of open businesses. Raises store.Unavailable
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
                if lead.business_status == CLOSED:
                    uids.append("")             # closed before anyone saw it: not added
                    continue
                row = _Row(uuid.uuid4().hex, None, [], set(), now, now)
                rows.append(row)
                new += 1
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
            if lead.business_status == CLOSED:
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
    shown = sum(l.business_status != CLOSED for l in leads)
    return new, shown - new


def is_closed(lead: Lead) -> bool:
    return lead.business_status == CLOSED


# The parsed saved list, per database: (fingerprint, leads ready to show). Every
# change to a saved row stamps its last_seen (save_search), so the row count and
# the sum and latest of those stamps say whether the list changed since it was
# parsed, in this process or another. Parsing 10,000 rows takes about 0.4 s; this
# check is one quick query.
_parsed: dict[str, tuple[store.Row | None, list[Lead]]] = {}
_parsed_lock = threading.Lock()


def _ready(row: _Row) -> Lead | None:
    """A saved row's lead as the pages show it (closed flag, uid, miles from Arco)."""
    lead = row.lead
    if lead is None:
        return None
    if is_closed(lead) and CLOSED_FLAG not in lead.flags:
        lead.flags = [CLOSED_FLAG, *lead.flags]
    lead.uid = row.uid
    lead.distance_miles = round(haversine_miles(*config.DEFAULT_CENTER, lead.lat, lead.lon), 2)
    return lead


def _best_first(leads: list[Lead]) -> list[Lead]:
    leads.sort(key=lambda l: (-l.score, l.distance_miles, l.name.lower()))
    return leads


def _whole_list(db: store.Db) -> list[Lead]:
    """Every saved lead, parsed once and reused until a search changes the list. Each
    call gets its own copies (marks and calls are set on them per request)."""
    fingerprint = db.one("SELECT COUNT(*), MAX(last_seen), SUM(last_seen) FROM leads")
    with _parsed_lock:
        cached = _parsed.get(db.key)
    if cached is None or cached[0] != fingerprint:
        rows = [_Row(uid, _to_lead(data) if (data := json.loads(lead)) else None, [],
                     set(), 0.0, 0.0)
                for uid, lead in db.all("SELECT uid, lead FROM leads")]
        cached = (fingerprint, _best_first([l for r in rows if (l := _ready(r))]))
        with _parsed_lock:
            _parsed[db.key] = cached
    return [copy.copy(lead) for lead in cached[1]]


def load(uids: Iterable[str] | None = None) -> list[Lead]:
    """All saved leads (best first), or with uids just those, with miles from
    Arco's shop. Their marks and calls are added by marks.apply / calls.apply.
    Businesses closed for good are included, flagged CLOSED_FLAG.

    Raises store.Unavailable without a database, or a database error when the
    leads can't be read.
    """
    now = time.time()
    wanted = None if uids is None else set(uids)
    with store.connect() as db:
        if config.SAVED_SOURCE_KEEP_SECONDS:
            rows = _read(db, None if wanted is None else list(wanted))
            _write(db, _drop_expired(rows, now))
        elif wanted is None:
            return _whole_list(db)
        else:
            # Nothing expires, so the source listings (most of each row) needn't be read.
            select = "SELECT uid, lead FROM leads"
            rows = [_Row(uid, _to_lead(data) if (data := json.loads(lead)) else None, [],
                         set(), 0.0, 0.0)
                    for uid, lead in store.rows_for(db, select, "uid", list(wanted))]
    return _best_first([l for r in rows
                        if (wanted is None or r.uid in wanted) and (l := _ready(r))])


def count() -> int:
    """How many saved leads the list shows (those closed for good too)."""
    with store.connect() as db:
        rows = db.all("SELECT lead FROM leads")
    return sum(1 for (lead,) in rows if json.loads(lead))


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
