"""The saved lead list: every search merges into it, one row per business.

A saved lead keeps the source listings it was built from ("parts"), for good (the
owner's decision, 2026-09-29: every source's details are kept, Yelp's and Google's
too). A new search's lead joins the saved one it matches (same source listing, or
the duplicate rules in dedupe.py) and replaces that source's listings with the
fresh ones, so a business never gets a second row and keeps its id (and its
baler mark).

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
from dataclasses import asdict, dataclass, fields
from typing import Any

from . import config, places, store
from .dedupe import Site, SiteIndex, duplicate_groups, is_duplicate, merge, same_site, site_of, snapshot
from .geo import haversine_miles
from .models import Lead
from .scoring import OWN_FLAG, OWN_FLAG_START, mark_needs_name, score_lead

_FIELDS = {f.name for f in fields(Lead)}
CLOSED = "CLOSED_PERMANENTLY"
# The flag a saved business that closed for good carries (on the page and in downloads).
CLOSED_FLAG = "Closed for good"
# One source listing kept with a saved lead: {"at": when it was found, "lead": its fields}.
Part = dict[str, Any]


def _to_lead(data: dict[str, Any]) -> Lead:
    return Lead(**{k: v for k, v in data.items() if k in _FIELDS})


# What a lead holds that isn't the listing's: its mark, calls and verified contact are
# read from their own tables each time (marks.py, calls.py, contacts.py), never stored here.
_NOT_STORED = ("parts", "has_baler", "mark_clicks", "last_call_at", "call_outcome", "call_notes", "call_count",
               "earlier_notes", "earlier_notes_at", "verified_phone", "contact_name", "contact_by", "contact_at",
               "contact_saves")


def _lead_json(lead: Lead) -> str:
    return json.dumps({k: v for k, v in asdict(lead).items() if k not in _NOT_STORED}, separators=(",", ":"))


def _part_id(part: Part) -> str:
    lead = part["lead"]
    return f"{lead.get('source')}:{lead.get('source_id')}"


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


def _read(db: store.Db) -> list[_Row]:
    """Every saved row, with its source listings."""
    rows = []
    for uid, lead, parts, ids, first, last in db.all(
            "SELECT uid, lead, parts, ids, first_seen, last_seen FROM leads"):
        data = json.loads(lead)
        rows.append(_Row(uid, _to_lead(data) if data else None, json.loads(parts),
                         set(json.loads(ids)), first, last))
    return rows


def _write(db: store.Db, rows: Iterable[_Row]) -> None:
    db.many("INSERT INTO leads (uid, lead, parts, ids, first_seen, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (uid) DO UPDATE SET lead = excluded.lead, "
            "parts = excluded.parts, ids = excluded.ids, last_seen = excluded.last_seen",
            [r.values() for r in rows])


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
        changed: dict[str, _Row] = {}
        by_id = {i: r for r in rows for i in r.ids}
        buckets: dict[tuple[float, float], list[_Row]] = {}
        for r in rows:
            if r.lead:
                buckets.setdefault(_bucket(r.lead), []).append(r)
        claimed: set[str] = set()
        uids: list[str] = []
        fresh: set[str] = set()                  # rows this search added
        sites = _SavedSites(rows)
        for lead in leads:
            parts = [{"at": now, "lead": p} for p in snapshot(lead)]
            ids = {_part_id(p) for p in parts}
            # Two leads the search kept apart never share a row, even via a listing id.
            # A lead with listings of two saved rows (saved apart before the search
            # rules joined them) joins the one saved first, the row merge-sites keeps.
            row = min((by_id[i] for i in ids if i in by_id and by_id[i].uid not in claimed),
                      key=lambda r: (r.first_seen, r.uid), default=None)
            if row is None:
                bx, by = _bucket(lead)
                near = [r for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                        for r in buckets.get((round(bx + dx * 0.01, 2), round(by + dy * 0.01, 2)), [])
                        if r.uid not in claimed and r.lead is not None and is_duplicate(lead, r.lead)]
                row = min(near, key=lambda r: _miles_between(lead, r.lead), default=None)
            if row is None:
                # A part of a site already saved ("Shoreline Ridge 830" beside the saved
                # "Shoreline Ridge", the base's airfield inside the saved base's outline):
                # it joins that row.
                row = sites.find(lead, claimed)
            if row is None:
                if lead.business_status == CLOSED:
                    uids.append("")             # closed before anyone saw it: not added
                    continue
                row = _Row(uuid.uuid4().hex, None, [], set(), now, now)
                rows.append(row)
                fresh.add(row.uid)
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
            sites.add(row)
        # Rows saved before sites were merged are joined only on request
        # (`python -m leadgen merge-sites`): that moves marks and calls between
        # saved rows, which the owner decides on, not an automatic save.
        _write(db, changed.values())
    for lead, uid in zip(leads, uids):
        lead.uid = uid
    new = len(fresh)
    shown = sum(l.business_status != CLOSED for l in leads)
    return new, max(0, shown - new)


def _row_site(row: _Row) -> Site | None:
    """The site a saved row is part of, placed (and outlined) by all its source listings."""
    if row.lead is None:
        return None
    return site_of(row.lead, [p["lead"] for p in row.parts])


class _SavedSites:
    """The saved rows by the site they are part of, to find the row a new lead's site
    joins (same_site) wherever its outline or a large site's spread puts it. Built on
    first use: most leads join their row by listing id or as a duplicate."""

    def __init__(self, rows: list[_Row]) -> None:
        self._rows = rows
        self._index: SiteIndex | None = None
        self._site: dict[str, Site | None] = {}

    def add(self, row: _Row) -> None:
        if self._index is None:
            return
        site = self._site[row.uid] = _row_site(row)
        if site is not None:
            self._index.add(row, site)

    def find(self, lead: Lead, claimed: set[str]) -> _Row | None:
        site = site_of(lead)
        if site is None:
            return None
        if self._index is None:
            self._index = SiteIndex()
            for row in self._rows:
                self.add(row)
        near = [r for r in self._index.near(site)
                if r.uid not in claimed and r.lead is not None
                and same_site(site, self._site.get(r.uid))]
        return min(near, key=lambda r: _miles_between(lead, r.lead), default=None)


def _same_business(rows: list[_Row]) -> list[tuple[_Row, list[_Row]]]:
    """The saved rows that are one business by the rules a search merges with
    (dedupe.duplicate_groups: duplicate listings, the parts of one site), or that share
    a source listing: [(the row saved first, the rows that would join it)]."""
    live = [r for r in rows if r.lead is not None and r.parts]
    listings: list[Lead] = []
    owner: list[int] = []
    together = []
    for k, r in enumerate(live):
        start = len(listings)
        for p in r.parts:
            listings.append(_to_lead(copy.deepcopy(p["lead"])))
            owner.append(k)
        together.append(list(range(start, len(listings))))
    parent = list(range(len(live)))

    def find(k: int) -> int:
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    for group in duplicate_groups(listings, together):
        for i in group[1:]:
            parent[find(owner[i])] = find(owner[group[0]])
    first: dict[str, int] = {}
    for k, r in enumerate(live):
        for pid in r.ids:
            parent[find(k)] = find(first.setdefault(pid, k))
    groups: dict[int, list[_Row]] = {}
    for k, r in enumerate(live):
        groups.setdefault(find(k), []).append(r)
    plan = []
    for members in groups.values():
        if len(members) > 1:
            members.sort(key=lambda r: (r.first_seen, r.uid))
            plan.append((members[0], members[1:]))
    return sorted(plan, key=lambda g: (g[0].first_seen, g[0].uid))


def _join(db: store.Db, plan: list[tuple[_Row, list[_Row]]], now: float,
          ) -> tuple[dict[str, str], list[_Row]]:
    """Merge each plan group's rows (see _same_business) into the row saved first.

    Every source listing, mark and call is kept: the listings join that row (so a
    later search finds it), the calls, the Yes / No clicks and the verified contacts
    move to it (the latest verified contact of the group shows), and it takes
    the latest mark (the older marks stay in its mark history), except that when some
    rows were marked Yes and others No it takes the latest Yes, and shows that its
    marks disagreed (marks.apply) until someone marks it again. The merged rows stay
    in the table, hidden (like a lead with no listings left), and are recorded in
    merged_leads as they were. Returns ({merged uid: uid it joined}, rows changed).
    """
    if not plan:
        return {}, []
    moved: dict[str, str] = {}
    changed: list[_Row] = []
    uids = [r.uid for keep, others in plan for r in [keep, *others]]
    with db.transaction():
        marked = {uid: (value, at) for uid, value, at in store.rows_for(
            db, "SELECT uid, value, updated_at FROM marks", "uid", uids)}
        for keep, others in plan:
            record = {r.uid: {"row": list(r.values()), "mark": marked.get(r.uid, ("", 0))[0]}
                      for r in others}
            seen = {_part_id(p) for p in keep.parts}
            for r in others:
                for p in r.parts:
                    if _part_id(p) not in seen:
                        keep.parts.append(p)
                        seen.add(_part_id(p))
                keep.ids |= r.ids
                r.lead, r.parts, r.ids, r.last_seen = None, [], set(), now
                moved[r.uid] = keep.uid
            keep.lead = _rebuild(keep.parts)
            keep.lead.uid = keep.uid
            keep.last_seen = now
            changed += [keep, *others]
            group_marks = [(marked[r.uid][1], marked[r.uid][0], r.uid) for r in [keep, *others]
                           if r.uid in marked]
            # One building having a baler usually means the site does: when the marks
            # disagree the site keeps Yes (the latest Yes), and the lead says so.
            disagreed = {value for _, value, _ in group_marks} >= {"yes", "no"}
            if disagreed:
                group_marks = [m for m in group_marks if m[1] == "yes"]
                for r in others:
                    record[r.uid]["marks_disagreed"] = True
            latest = max(group_marks, default=None)
            if latest and latest[2] != keep.uid:
                db.run("INSERT INTO marks (uid, value, updated_at) VALUES (?, ?, ?) "
                       "ON CONFLICT (uid) DO UPDATE SET value = excluded.value, "
                       "updated_at = excluded.updated_at", (keep.uid, latest[1], latest[0]))
            for r in others:
                for table in ("calls", "call_undos", "mark_changes", "lead_contacts"):
                    db.run(f"UPDATE {table} SET uid = ? WHERE uid = ?", (keep.uid, r.uid))
                db.run("INSERT INTO merged_leads (uid, into_uid, at, row) VALUES (?, ?, ?, ?) "
                       "ON CONFLICT (uid) DO NOTHING",
                       (r.uid, keep.uid, now, json.dumps(record[r.uid], separators=(",", ":"))))
        for r in changed:
            db.run("INSERT INTO leads (uid, lead, parts, ids, first_seen, last_seen) "
                   "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (uid) DO UPDATE SET lead = excluded.lead, "
                   "parts = excluded.parts, ids = excluded.ids, last_seen = excluded.last_seen",
                   r.values())
    return moved, changed


@dataclass
class PlannedRow:
    """A saved row in a merge plan, as `python -m leadgen merge-sites` lists it."""

    uid: str
    name: str
    city: str
    saved_at: float
    mark: str            # "yes", "no" or ""
    calls: int


def merge_plan() -> list[list[PlannedRow]]:
    """The saved leads that are one business (see _same_business), each group's first row
    the one the others would join. Changes nothing."""
    with store.connect() as db:
        plan = _same_business(_read(db))
        uids = [r.uid for keep, others in plan for r in [keep, *others]]
        marked = {uid: value for uid, value in store.rows_for(
            db, "SELECT uid, value FROM marks", "uid", uids)}
        called: dict[str, int] = {}
        for (uid,) in store.rows_for(db, "SELECT uid FROM calls", "uid", uids):
            called[uid] = called.get(uid, 0) + 1
    return [[PlannedRow(r.uid, r.lead.name, _town(r.lead), r.first_seen, marked.get(r.uid, ""),
                        called.get(r.uid, 0))
             for r in [keep, *others] if r.lead] for keep, others in plan]


def _town(lead: Lead) -> str:
    """The lead's city, or the town its pin is near ("near Layton")."""
    town = places.listed_town(lead)
    if town:
        return town
    found = places.near(lead.lat, lead.lon)
    return f"near {found.town}" if found else ""


def merge_sites() -> int:
    """Merge the saved leads that are one business (see _same_business and _join);
    returns how many rows were merged into another. Only run on request, from the
    command line (`python -m leadgen merge-sites --apply`)."""
    with store.connect() as db:
        moved, _ = _join(db, _same_business(_read(db)), time.time())
    return len(moved)


def is_closed(lead: Lead) -> bool:
    return lead.business_status == CLOSED


# The parsed saved list, per database: (fingerprint, leads ready to show). Every
# change to a saved row stamps its last_seen (save_search), so the row count and
# the sum and latest of those stamps say whether the list changed since it was
# parsed, in this process or another. Parsing 10,000 rows takes about 0.4 s; this
# check is one quick query.
_parsed: dict[str, tuple[store.Row | None, list[Lead]]] = {}
_parsed_lock = threading.Lock()
_parsing = threading.Lock()


def _ready(row: _Row) -> Lead | None:
    """A saved row's lead as the pages show it (closed flag, uid, miles from AARCO)."""
    lead = row.lead
    if lead is None:
        return None
    if is_closed(lead) and CLOSED_FLAG not in lead.flags:
        lead.flags = [CLOSED_FLAG, *lead.flags]
    if any(f.startswith(OWN_FLAG_START) and f != OWN_FLAG for f in lead.flags):
        # Saved before the owner corrected the company's name ("Arco"): shown with the name
        # it has now. Only what is shown changes; the saved row keeps its text.
        lead.flags = [OWN_FLAG if f.startswith(OWN_FLAG_START) else f for f in lead.flags]
    # A building with no business name, address or phone, saved before such rows were held
    # back: shown in tier D and flagged, as a search now scores it (the saved row is unchanged).
    mark_needs_name(lead)
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
        # One parse at a time: a request that comes in meanwhile (the first visitor while
        # the site gets ready, awake.py) waits for it and takes its result.
        with _parsing:
            with _parsed_lock:
                cached = _parsed.get(db.key)
            if cached is None or cached[0] != fingerprint:
                rows = [_Row(uid, _to_lead(data) if (data := json.loads(lead)) else None, [],
                             set(), 0.0, 0.0)
                        for uid, lead in db.all("SELECT uid, lead FROM leads")]
                cached = (fingerprint, _best_first([l for r in rows if (l := _ready(r))]))
                with _parsed_lock:
                    _parsed[db.key] = cached
    return [_shallow(lead) for lead in cached[1]]


def _shallow(lead: Lead) -> Lead:
    """A shallow copy (like copy.copy, about three times faster over a long list)."""
    new = Lead.__new__(Lead)
    new.__dict__.update(lead.__dict__)
    return new


def load(uids: Iterable[str] | None = None) -> list[Lead]:
    """All saved leads (best first), or with uids just those, with miles from
    AARCO's shop. Their marks and calls are added by marks.apply / calls.apply.
    Businesses closed for good are included, flagged CLOSED_FLAG.

    Raises store.Unavailable without a database, or a database error when the
    leads can't be read.
    """
    if uids is None:
        with store.connect() as db:
            return _whole_list(db)
    wanted = set(uids)
    with store.connect() as db:
        # The source listings (most of each row) needn't be read.
        select = "SELECT uid, lead FROM leads"
        rows = [_Row(uid, _to_lead(data) if (data := json.loads(lead)) else None, [],
                     set(), 0.0, 0.0)
                for uid, lead in store.rows_for(db, select, "uid", list(wanted))]
    return _best_first([l for r in rows if r.uid in wanted and (l := _ready(r))])


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
    """(first, latest): when leads were first and last saved (epoch seconds; a search
    saves its leads at its end), or (None, None) for an empty list."""
    with store.connect() as db:
        row = db.one("SELECT MIN(first_seen), MAX(last_seen) FROM leads")
    return (row[0], row[1]) if row else (None, None)
