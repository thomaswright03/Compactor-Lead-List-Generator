"""Merge duplicate listings (same business found by several queries or sources).

Whether two names or phone numbers say one business is in dedupe_names.py, and the
parts of one site (an apartment complex's buildings, a campus) in dedupe_sites.py;
every name they had here is still imported from here.
"""

import copy
import re
from collections.abc import Callable, Iterable
from dataclasses import asdict
from typing import Any

from .dedupe_names import (
    _GENERIC,
    _PART_WORDS,
    SAME_NAME_MILES,
    SAME_PHONE_MILES,
    SAME_PLACE_MILES,
    _numbers,
    clean_name,
    names_match,
    part_added,
    phone_numbers,
    site_name,
    town_added,
)
from .dedupe_sites import (
    LARGE_SITE_MILES,
    OUTLINE_MARGIN_MILES,
    OUTLINE_MAX_MILES,
    SITE_MILES,
    Box,
    Cell,
    Point,
    Site,
    SiteIndex,
    _within,
    large_site,
    outline_box,
    same_site,
    site_groups,
    site_of,
)
from .geo import haversine_miles
from .models import Lead
from .scoring import is_non_prospect_tag

# Re-exported: callers import these from dedupe, wherever they are written.
__all__ = ["LARGE_SITE_MILES", "OUTLINE_MARGIN_MILES", "OUTLINE_MAX_MILES", "SAME_NAME_MILES", "SAME_PHONE_MILES",
           "SAME_PLACE_MILES", "SITE_MILES", "Box", "Cell", "Point", "Site", "SiteIndex", "clean_name",
           "large_site", "names_match", "outline_box", "part_added", "phone_numbers", "same_site", "site_groups",
           "site_name", "site_of", "town_added"]

# Map tags that say what business a listing is, not only what its building is: a map
# listing with none of them (only building=*, landuse=*...) is just a building's outline
# (building_only).
_BUSINESS_TAG_KEYS = frozenset({"shop", "amenity", "office", "craft", "industrial", "man_made", "tourism",
                                "leisure", "aeroway", "healthcare", "military", "brand"})
# Sources that report whether a place is open, most trusted first. The merged
# record is built on the first of these (best phone/website coverage).
PAID_SOURCES = ("google", "yelp")


def building_only(lead: Lead) -> bool:
    """True for a map listing that is only a building's outline: none of its tags says
    what business is there (_BUSINESS_TAG_KEYS), and it has no phone or website."""
    if lead.source != "osm" or lead.phone or lead.website:
        return False
    return not any(c.partition("=")[0] in _BUSINESS_TAG_KEYS for c in lead.raw_categories)


def part_of_business(building: Lead, business: Lead) -> bool:
    """A building's own map listing named for a part of the business it belongs to
    ("Cancer Hospital South" beside "Huntsman Cancer Hospital"): the building's listing
    is only an outline (building_only) and the business's is not; the building's name,
    less words naming a part of a site (_PART_WORDS), is within the business's name, and
    the business's name adds who it is."""
    if not building_only(building) or building_only(business):
        return False
    short = {t for t in clean_name(building.name).split() if t not in _PART_WORDS}
    longer = set(clean_name(business.name).split())
    return bool(short - _GENERIC) and short < longer and bool(longer - short - _GENERIC - _PART_WORDS)


def _on_or_beside(building: Lead, business: Lead, dist: float) -> bool:
    """The building lies within SAME_PLACE_MILES of the business's pin, or inside its outline."""
    if dist <= SAME_PLACE_MILES:
        return True
    box = outline_box(business.outline)
    return box is not None and _within([(building.lat, building.lon)], [box])


def is_duplicate(a: Lead, b: Lead) -> bool:
    if a.source == b.source and a.source_id == b.source_id:
        return True
    dist = haversine_miles(a.lat, a.lon, b.lat, b.lon)
    if dist > SAME_PHONE_MILES:
        return False          # every rule below needs the pins within half a mile
    pa, pb = phone_numbers(a.phone), phone_numbers(b.phone)
    shared_phone = bool(pa & pb)
    # "Building 1" / "Building 2", "343 Apartments" / "525 Apartments".
    na, nb = _numbers(a.name), _numbers(b.name)
    if na and nb and na.isdisjoint(nb) and not shared_phone:
        return False
    ca = clean_name(a.name)
    phones_conflict = pa and pb and not shared_phone
    # Big sites: a store's entrance pin and its building outline can be ~300 m apart.
    if (ca and ca == clean_name(b.name) and dist <= SAME_NAME_MILES
            and (not phones_conflict or set(ca.split()) - _GENERIC)):
        return True
    if phones_conflict:
        return False          # different phone numbers: different businesses
    if dist <= SAME_PLACE_MILES and names_match(a.name, b.name):
        return True
    # A building's outline named for part of the business it lies on or beside.
    if any(part_of_business(x, y) and _on_or_beside(x, y, dist) for x, y in ((a, b), (b, a))):
        return True
    if not shared_phone:
        return False
    return dist <= 0.05 or names_match(a.name, b.name)


def _resolve_status(group: list[Lead]) -> str:
    """Google's word wins, then Yelp's: closed only if none of that source's listings is open."""
    for source in PAID_SOURCES:
        said = {l.business_status for l in group if l.source == source and l.business_status}
        for status in ("OPERATIONAL", "CLOSED_TEMPORARILY", "CLOSED_PERMANENTLY"):
            if status in said:
                return status
    return next((l.business_status for l in group if l.business_status), "")


def _source_rank(lead: Lead) -> int:
    return PAID_SOURCES.index(lead.source) if lead.source in PAID_SOURCES else len(PAID_SOURCES)


def snapshot(lead: Lead) -> list[dict[str, Any]]:
    """The listing as its source sent it (the parts a merged lead is made of)."""
    return lead.parts or [{k: v for k, v in asdict(lead).items() if k != "parts"}]


def _site_title(parts: list[dict[str, Any]], name: str) -> str:
    """The name a lead merged from several buildings of one site shows: without the
    building number ("Shoreline Ridge 825" + "826" -> "Shoreline Ridge")."""
    names = {str(p.get("name") or "") for p in parts}
    if len(names) < 2 or len({_numbers(n) for n in names}) < 2:
        return name
    if len({site_name(n) for n in names}) != 1 or not site_name(name):
        return name
    title = re.sub(r"(?:^|\s)#?\s*\d+[A-Za-z]?(?=\s|$|[,)])", "", name)
    title = re.sub(r"\s+", " ", title).strip(" -,#")
    return title if re.search(r"[A-Za-z]", title) else name


def merge(group: list[Lead]) -> Lead:
    parts = [p for lead in group for p in snapshot(lead)]
    # Open listings first, then Google, then Yelp records (phone/website coverage), then
    # a business's own listing before a building's outline (the business's name is the
    # lead's: "Huntsman Cancer Hospital", not its building "Cancer Hospital South"), then
    # the richest.
    group.sort(key=lambda l: (l.business_status == "CLOSED_PERMANENTLY", _source_rank(l), building_only(l),
                              -sum(bool(x) for x in (l.phone, l.website, l.address, l.zip))))
    base = group[0]
    for other in group[1:]:
        for attr in ("address", "city", "state", "zip", "phone", "website", "primary_category",
                     "map_url"):
            if not getattr(base, attr) and getattr(other, attr):
                setattr(base, attr, getattr(other, attr))
        if other.rating_count and (base.rating_count or 0) < other.rating_count:
            base.rating_count = other.rating_count
        if other.yelp_reviews and (base.yelp_reviews or 0) < other.yelp_reviews:
            base.yelp_reviews = other.yelp_reviews
        if other.footprint_sqft and (base.footprint_sqft or 0) < other.footprint_sqft:
            base.footprint_sqft = other.footprint_sqft
        # A merged parking lot or pharmacy tag must not veto the main listing.
        for c in other.raw_categories:
            if c not in base.raw_categories and not is_non_prospect_tag(c):
                base.raw_categories.append(c)
        for t in other.search_terms:
            if t not in base.search_terms:
                base.search_terms.append(t)
        # Keep merged names/sites so a competitor never hides behind another record.
        for extra in [other.name, other.website] + other.alt_names:
            if extra and extra not in (base.name, base.website) and extra not in base.alt_names:
                base.alt_names.append(extra)
    base.business_status = _resolve_status(group)
    base.sources = sorted({l.source for l in group})
    base.parts = parts if len(parts) > 1 else []
    title = _site_title(parts, base.name)
    if title != base.name:
        base.alt_names = [n for n in base.alt_names if n not in (title, base.name)] + [base.name]
        base.name = title
    return base


def merge_sites(leads: list[Lead]) -> list[Lead]:
    """Merge the listings that are parts of one named site into one lead: the
    numbered buildings of an apartment complex ("Shoreline Ridge 825" ... "834"),
    a same-named site whose outlines spread wider than SAME_NAME_MILES, a campus's
    "Center" and "Campus" listings. Separate businesses whose names are only numbers
    and generic words ("343 Apartments", "525 Apartments") stay apart."""
    groups = site_groups([site_of(lead) for lead in leads])
    if not groups:
        return leads
    joined = {i for g in groups for i in g}
    out = [lead for i, lead in enumerate(leads) if i not in joined]
    for g in groups:
        out.append(merge([leads[i] for i in g]))
    return out


def _pair_rank(a: Lead, b: Lead) -> tuple[int, float, float]:
    """Merge order: strongest evidence, then the main (most reviewed) listing, then closest."""
    strength = (0 if clean_name(a.name) == clean_name(b.name)
                else 1 if phone_numbers(a.phone) & phone_numbers(b.phone) else 2)
    return (strength, -max(a.rating_count or 0, b.rating_count or 0,
                           a.yelp_reviews or 0, b.yelp_reviews or 0),
            haversine_miles(a.lat, a.lon, b.lat, b.lon))


def _same_listing_groups(leads: list[Lead], union: Callable[[int, int], None],
                         find: Callable[[int], int]) -> None:
    """Pre-merge big groups of copies of one listing (same cleaned name, phones and
    numbers, all within SAME_NAME_MILES of each other): every pair of them is a
    duplicate, so they are one group, found without comparing every pair (a chain
    with hundreds of copies would otherwise take seconds)."""
    by_sig: dict[tuple[str, frozenset[str], frozenset[str]], list[int]] = {}
    for i, lead in enumerate(leads):
        name = clean_name(lead.name)
        if name:
            by_sig.setdefault((name, phone_numbers(lead.phone), _numbers(lead.name)),
                              []).append(i)
    for idxs in by_sig.values():
        if len(idxs) < _PREMERGE_MIN:
            continue
        # Greedy clusters whose bounding box fits inside SAME_NAME_MILES: any two
        # members of one are within range of each other.
        idxs.sort(key=lambda i: (leads[i].lat, leads[i].lon))
        cluster: list[int] = []
        box = None
        for at in [*idxs, None]:
            if at is not None:
                lat, lon = leads[at].lat, leads[at].lon
                grown = (lat, lon, lat, lon) if box is None else (
                    min(box[0], lat), min(box[1], lon), max(box[2], lat), max(box[3], lon))
                if haversine_miles(*grown) <= _PREMERGE_MILES:
                    cluster.append(at)
                    box = grown
                    continue
            for j in cluster[1:]:
                ri, rj = find(cluster[0]), find(j)
                if ri != rj:
                    union(ri, rj)
            if at is not None:
                cluster, box = [at], (lat, lon, lat, lon)


# Groups this big are pre-merged (smaller ones go through the pairwise rules).
_PREMERGE_MIN = 8
_PREMERGE_MILES = SAME_NAME_MILES * 0.9


# A candidate duplicate pair: (its merge rank, see _pair_rank; one lead's index; the other's).
Pair = tuple[tuple[int, float, float], int, int]


class _Groups:
    """Union-find over lead indexes: find(i) is i's group, members[root] its indexes."""

    def __init__(self, n: int) -> None:
        self.parent = list(range(n))
        self.members = {i: [i] for i in range(n)}

    def find(self, i: int) -> int:
        parent = self.parent
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(self, ri: int, rj: int) -> None:
        self.parent[rj] = ri
        self.members[ri] += self.members.pop(rj)


def _grouped(leads: list[Lead], together: Iterable[list[int]] | None = None,
             ) -> tuple[_Groups, list[tuple[int, int]]]:
    """Group the duplicate listings (see dedupe); together: groups of indexes that start
    as one (the listings of one saved lead). Returns the groups and the duplicate pairs
    that complete linkage kept apart."""
    g = _Groups(len(leads))
    # Same record from the same source (found by several queries) is always one place.
    seen: dict[tuple[str, str], int] = {}
    for i, lead in enumerate(leads):
        key = (lead.source, lead.source_id)
        if key in seen:
            ri, rj = g.find(seen[key]), g.find(i)
            if ri != rj:
                g.union(ri, rj)
        else:
            seen[key] = i
    if together is None:
        _same_listing_groups(leads, g.union, g.find)
    else:
        # Saved leads: each is one group from the start, and two of them join only when
        # every listing of one is a duplicate of every listing of the other.
        for members in together:
            for j in members[1:]:
                ri, rj = g.find(members[0]), g.find(j)
                if ri != rj:
                    g.union(ri, rj)
    return g, _link(leads, g, _candidate_pairs(leads, g))


def duplicate_groups(leads: list[Lead], together: Iterable[list[int]] | None = None,
                     ) -> list[list[int]]:
    """The groups of listings (by index) that dedupe would make one lead, by the same
    rules: duplicates, then the parts of one site. together: as for _grouped. Returns
    the groups of more than one listing."""
    g, _ = _grouped(leads, together)
    groups: dict[int, list[int]] = {}
    for i in range(len(leads)):
        groups.setdefault(g.find(i), []).append(i)
    found = list(groups.values())
    merged = [merge([copy.deepcopy(leads[i]) for i in idxs]) for idxs in found]
    joined: set[int] = set()
    out = []
    for group in site_groups([site_of(lead) for lead in merged]):
        out.append(sorted(i for k in group for i in found[k]))
        joined.update(group)
    out += [idxs for k, idxs in enumerate(found) if k not in joined and len(idxs) > 1]
    return sorted(out)


def dedupe(leads: list[Lead]) -> list[Lead]:
    """Return merged leads.

    Candidate pairs are merged strongest first, and two groups merge only when
    every member of one is a duplicate of every member of the other, so A~B and
    B~C never chain A and C together and the result does not depend on input
    order. A spatial grid keeps it fast on thousands of rows, and many copies of
    one listing are grouped up front (see _same_listing_groups).
    """
    g, links = _grouped(leads)
    groups: dict[int, list[Lead]] = {}
    for i in range(len(leads)):
        groups.setdefault(g.find(i), []).append(leads[i])
    closed = _closed_by_links(links, groups, g)
    out = []
    for root, group in groups.items():
        lead = merge(group)
        if root in closed:
            lead.business_status = "CLOSED_PERMANENTLY"
        out.append(lead)
    return merge_sites(out)


def _candidate_pairs(leads: list[Lead], g: _Groups) -> list[Pair]:
    """Duplicate pairs in different groups, strongest first, found on a spatial grid."""
    buckets: dict[tuple[float, float], list[int]] = {}
    for i, lead in enumerate(leads):
        buckets.setdefault((round(lead.lat, 2), round(lead.lon, 2)), []).append(i)
    pairs = []
    for (bx, by), idxs in buckets.items():
        # Neighbours by group, so a big pre-merged group is skipped in one step.
        near: dict[int, list[int]] = {}
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for j in buckets.get((round(bx + dx * 0.01, 2), round(by + dy * 0.01, 2)), []):
                    near.setdefault(g.find(j), []).append(j)
        for i in idxs:
            ri = g.find(i)
            for rj, js in near.items():
                if rj == ri:
                    continue
                for j in js:
                    if j > i and is_duplicate(leads[i], leads[j]):
                        pairs.append((_pair_rank(leads[i], leads[j]), i, j))
    pairs.sort()
    return pairs


def _link(leads: list[Lead], g: _Groups, pairs: list[Pair]) -> list[tuple[int, int]]:
    """Merge the pairs' groups when every member of one is a duplicate of every member
    of the other; returns the duplicate pairs that complete linkage kept apart."""
    links = []
    rejected = set()      # group pairs already found not to match (groups only grow)
    for _, i, j in pairs:
        ri, rj = g.find(i), g.find(j)
        if ri == rj:
            continue
        state = (ri, rj, len(g.members[ri]), len(g.members[rj]))
        if state in rejected:
            links.append((i, j))
            continue
        if all(is_duplicate(leads[x], leads[y]) for x in g.members[ri] for y in g.members[rj]):
            g.union(ri, rj)
        else:
            rejected.add(state)
            links.append((i, j))
    return links


def _closed_by_links(links: list[tuple[int, int]], groups: dict[int, list[Lead]],
                     g: _Groups) -> set[int]:
    """A copy linked to a listing from a more trusted source (Google over Yelp over
    the map) that reports it permanently closed is closed too, unless it is also
    linked to one such listing that reports it open. Returns those groups' roots."""
    def trust(group: list[Lead]) -> int:
        return min(_source_rank(l) for l in group)

    closed, still_open = set(), set()
    for i, j in links:
        ri, rj = g.find(i), g.find(j)
        for a, b in ((ri, rj), (rj, ri)):
            if a == b or trust(groups[a]) >= trust(groups[b]):
                continue
            status = _resolve_status(groups[a])
            if status == "CLOSED_PERMANENTLY":
                closed.add(b)
            elif status == "OPERATIONAL":
                still_open.add(b)
    return closed - still_open
