"""Merge duplicate listings (same business found by several queries or sources)."""

import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from functools import lru_cache
from typing import Any

from .geo import haversine_miles
from .models import Lead
from .scoring import is_non_prospect_tag, normalize

_STOPWORDS = {"the", "inc", "llc", "co", "corp", "corporation", "company", "ltd", "store",
              "of", "and", "at", "utah", "ut", "slc"}
# Words that say what or where a place is, not who it is. Sharing only these
# ("Comfort Inn & Suites Airport" / "Fairfield Inn & Suites Airport") is not a match.
_GENERIC = {"apartments", "apartment", "apts", "main", "downtown", "recycling", "warehouse",
            "center", "centre", "hotel", "inn", "suites", "motel", "airport", "market",
            "plaza", "building", "office", "station", "north", "south", "east", "west",
            "salt", "lake", "city", "valley", "on", "by", "a", "in", "for", "street", "st",
            "avenue", "ave", "road", "rd", "park", "medical", "services", "service", "group"}
SAME_PLACE_MILES = 0.12      # ~200 m: two listings this close with similar names are one site
SAME_NAME_MILES = 0.2        # identical cleaned names
SAME_PHONE_MILES = 0.5       # the widest merge distance of all rules
# One named site spread over many listings (the buildings of an apartment complex,
# the parts of a campus): each listing within SAME_NAME_MILES of the next, the whole
# site at most SITE_MILES across (see merge_sites).
SITE_MILES = 0.5
# Words that name a part of a site, not the site ("Westpointe Center" / "Westpointe
# Campus", "Adagio Building A"), left out of the site's name.
_SITE_WORDS = {"campus", "center", "centre", "complex", "building", "buildings", "bldg",
               "unit", "units", "phase", "tower", "towers", "wing"}
# Sources that report whether a place is open, most trusted first. The merged
# record is built on the first of these (best phone/website coverage).
PAID_SOURCES = ("google", "yelp")


@lru_cache(maxsize=65536)
def clean_name(name: str) -> str:
    tokens = [t for t in normalize(name).split() if t not in _STOPWORDS and not t.isdigit()]
    return " ".join(tokens)


@lru_cache(maxsize=65536)
def _numbers(name: str) -> frozenset[str]:
    return frozenset(t for t in normalize(name).split() if t.isdigit())


@lru_cache(maxsize=65536)
def phone_numbers(phone: str) -> frozenset[str]:
    """Every 10-digit number in a tag like '+1 801-555-0100;+1 801-555-0199 ext. 2'."""
    nums = set()
    for part in re.split(r"[;,/]|\bor\b", phone or "", flags=re.IGNORECASE):
        part = re.split(r"(?:ext\.?|extension|x|#)\s*\d", part, maxsplit=1,
                        flags=re.IGNORECASE)[0]
        digits = re.sub(r"\D", "", part)
        if len(digits) >= 10:
            nums.add(digits[-10:])
    return frozenset(nums)


def names_match(a: str, b: str) -> bool:
    ca, cb = clean_name(a), clean_name(b)
    if not ca or not cb:
        return False
    if ca == cb:
        return True
    ta, tb = set(ca.split()), set(cb.split())
    small = ta if len(ta) <= len(tb) else tb
    if (ta <= tb or tb <= ta) and small - _GENERIC:
        return True
    da = " ".join(t for t in ca.split() if t not in _GENERIC)
    db = " ".join(t for t in cb.split() if t not in _GENERIC)
    # Both the distinctive part and the full name must be close.
    return (bool(da and db) and SequenceMatcher(None, da, db).ratio() >= 0.85
            and SequenceMatcher(None, ca, cb).ratio() >= 0.85)


@lru_cache(maxsize=65536)
def site_name(name: str) -> str:
    """The name of the site a listing is part of: its cleaned name without building
    numbers or letters and without words like "campus" or "building" ("Shoreline Ridge
    825" -> "shoreline ridge"). "" when that leaves only generic words ("343
    Apartments", "Building 2"): such names say nothing about which site it is."""
    tokens = [t for t in normalize(name).split()
              if t not in _STOPWORDS and t not in _SITE_WORDS and len(t) > 1
              and not re.fullmatch(r"\d+[a-z]?", t)]
    return " ".join(tokens) if set(tokens) - _GENERIC else ""


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
    # Open listings first, then Google, then Yelp records (phone/website coverage),
    # then the richest.
    group.sort(key=lambda l: (l.business_status == "CLOSED_PERMANENTLY", _source_rank(l),
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


@dataclass(frozen=True)
class Site:
    """What says which site a lead (or a saved row) is part of: its site name, its
    phone numbers and where its listings are."""

    name: str
    phones: frozenset[str]
    points: tuple[tuple[float, float], ...]


def site_of(lead: Lead, points: list[tuple[float, float]] | None = None) -> Site | None:
    """The lead's Site (points: where its listings are; default its parts, or its pin),
    or None when its name says nothing about which site it is."""
    name = site_name(lead.name)
    if not name:
        return None
    if points is None:
        points = [(float(p["lat"]), float(p["lon"])) for p in lead.parts
                  if p.get("lat") is not None and p.get("lon") is not None]
    return Site(name, phone_numbers(lead.phone), tuple(points or [(lead.lat, lead.lon)]))


def _box(points: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    lats, lons = [p[0] for p in points], [p[1] for p in points]
    return min(lats), min(lons), max(lats), max(lons)


def _phones_agree(a: frozenset[str], b: frozenset[str]) -> bool:
    return not (a and b and a.isdisjoint(b))


def _gap(a: Site, b: Site) -> float:
    """Miles between the nearest listings of two sites."""
    return min(haversine_miles(p[0], p[1], q[0], q[1]) for p in a.points for q in b.points)


def same_site(a: Site | None, b: Site | None) -> bool:
    """Two parts of one named site: the same site name, no phone numbers that differ,
    a listing of one within SAME_NAME_MILES of a listing of the other, and the two
    together at most SITE_MILES across."""
    if a is None or b is None or a.name != b.name or not _phones_agree(a.phones, b.phones):
        return False
    if haversine_miles(*_box(list(a.points + b.points))) > SITE_MILES:
        return False
    return _gap(a, b) <= SAME_NAME_MILES


def site_groups(sites: list[Site | None]) -> list[list[int]]:
    """Group the sites that are parts of one site (same_site, closest first), keeping
    every group within SITE_MILES across and free of differing phone numbers. Returns
    the groups of more than one, by index."""
    by_name: dict[str, list[int]] = {}
    for i, site in enumerate(sites):
        if site is not None:
            by_name.setdefault(site.name, []).append(i)
    out = []
    lat_span = SITE_MILES / 69.0 + 1e-9
    for idxs in by_name.values():
        if len(idxs) < 2:
            continue
        pairs = []
        idxs.sort(key=lambda i: min(p[0] for p in sites[i].points))    # type: ignore[union-attr]
        for n, i in enumerate(idxs):
            a = sites[i]
            assert a is not None
            top = max(p[0] for p in a.points)
            for j in idxs[n + 1:]:
                b = sites[j]
                assert b is not None
                if min(p[0] for p in b.points) - top > lat_span:
                    break
                if same_site(a, b):
                    pairs.append((_gap(a, b), i, j))
        pairs.sort()
        group = {i: [i] for i in idxs}
        root = {i: i for i in idxs}
        for _, i, j in pairs:
            ri, rj = root[i], root[j]
            if ri == rj:
                continue
            members = group[ri] + group[rj]
            points = [p for m in members for p in sites[m].points]    # type: ignore[union-attr]
            if haversine_miles(*_box(points)) > SITE_MILES:
                continue
            if not all(_phones_agree(sites[x].phones, sites[y].phones)   # type: ignore[union-attr]
                       for x in group[ri] for y in group[rj]):
                continue
            group[ri] = members
            for m in group.pop(rj):
                root[m] = ri
        out += [sorted(g) for g in group.values() if len(g) > 1]
    return out


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


def dedupe(leads: list[Lead]) -> list[Lead]:
    """Return merged leads.

    Candidate pairs are merged strongest first, and two groups merge only when
    every member of one is a duplicate of every member of the other, so A~B and
    B~C never chain A and C together and the result does not depend on input
    order. A spatial grid keeps it fast on thousands of rows, and many copies of
    one listing are grouped up front (see _same_listing_groups).
    """
    parent = list(range(len(leads)))
    members = {i: [i] for i in range(len(leads))}

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(ri: int, rj: int) -> None:
        parent[rj] = ri
        members[ri] += members.pop(rj)

    # Same record from the same source (found by several queries) is always one place.
    seen: dict[tuple[str, str], int] = {}
    for i, lead in enumerate(leads):
        key = (lead.source, lead.source_id)
        if key in seen:
            ri, rj = find(seen[key]), find(i)
            if ri != rj:
                union(ri, rj)
        else:
            seen[key] = i
    _same_listing_groups(leads, union, find)

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
                    near.setdefault(find(j), []).append(j)
        for i in idxs:
            ri = find(i)
            for rj, js in near.items():
                if rj == ri:
                    continue
                for j in js:
                    if j > i and is_duplicate(leads[i], leads[j]):
                        pairs.append((_pair_rank(leads[i], leads[j]), i, j))
    pairs.sort()

    links = []            # duplicate pairs that complete linkage kept apart
    rejected = set()      # group pairs already found not to match (groups only grow)
    for _, i, j in pairs:
        ri, rj = find(i), find(j)
        if ri == rj:
            continue
        state = (ri, rj, len(members[ri]), len(members[rj]))
        if state in rejected:
            links.append((i, j))
            continue
        if all(is_duplicate(leads[x], leads[y]) for x in members[ri] for y in members[rj]):
            union(ri, rj)
        else:
            rejected.add(state)
            links.append((i, j))

    groups: dict[int, list[Lead]] = {}
    for i in range(len(leads)):
        groups.setdefault(find(i), []).append(leads[i])

    # A copy linked to a listing from a more trusted source (Google over Yelp over
    # the map) that reports it permanently closed is closed too, unless it is also
    # linked to one such listing that reports it open.
    def trust(group: list[Lead]) -> int:
        return min(_source_rank(l) for l in group)

    closed, still_open = set(), set()
    for i, j in links:
        ri, rj = find(i), find(j)
        for a, b in ((ri, rj), (rj, ri)):
            if a == b or trust(groups[a]) >= trust(groups[b]):
                continue
            status = _resolve_status(groups[a])
            if status == "CLOSED_PERMANENTLY":
                closed.add(b)
            elif status == "OPERATIONAL":
                still_open.add(b)
    closed -= still_open

    out = []
    for root, group in groups.items():
        lead = merge(group)
        if root in closed:
            lead.business_status = "CLOSED_PERMANENTLY"
        out.append(lead)
    return merge_sites(out)
