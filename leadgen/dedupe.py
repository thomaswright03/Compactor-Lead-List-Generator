"""Merge duplicate listings (same business found by several queries or sources)."""

import copy
import math
import re
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from functools import lru_cache
from typing import Any

from . import config, places
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
# A listing inside the map outline of another listing of the same site is part of that
# site, however far apart their pins are ("Hill Air Force Base" as an airfield and as a
# base; a distribution centre's buildings inside its yard). A pin just outside the
# outline (a gate) counts within OUTLINE_MARGIN_MILES; an outline more than
# OUTLINE_MAX_MILES across (a forest, a county park) says nothing about one site.
OUTLINE_MARGIN_MILES = SAME_NAME_MILES
OUTLINE_MAX_MILES = 8.0
# Two same-named listings of a site that spreads over a mile or more (an air base, an
# airport, a campus: config.LARGE_SITE_OSM_TAGS) are one site within this distance, even
# without an outline (rows saved before outlines were kept).
LARGE_SITE_MILES = 1.5
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
    if town_added(ca, cb):
        return True
    da = " ".join(t for t in ca.split() if t not in _GENERIC)
    db = " ".join(t for t in cb.split() if t not in _GENERIC)
    # Both the distinctive part and the full name must be close.
    return (bool(da and db) and SequenceMatcher(None, da, db).ratio() >= 0.85
            and SequenceMatcher(None, ca, cb).ratio() >= 0.85)


@lru_cache(maxsize=1)
def _towns() -> re.Pattern[str]:
    """Utah's town names (places.py), longest first, as whole words of a normalized name."""
    names = sorted({normalize(n) for n in places.town_names()} - {""}, key=len, reverse=True)
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(map(re.escape, names)) + r")(?![a-z0-9])")


@lru_cache(maxsize=65536)
def _without_towns(name: str) -> str:
    return " ".join(_towns().sub(" ", name).split())


def town_added(a: str, b: str) -> bool:
    """True when one cleaned name is the other with a town written into it ("smiths
    distribution center" / "smiths layton distribution"): only the second names a town,
    and without it both have the same two or more words that say who they are (words
    like "center" aside). Two names that each name a town ("layton auto parts" /
    "kaysville auto parts") are two towns' businesses, and one word left ("daniel
    construction" / "construction center") says too little."""
    plain_a, plain_b = _without_towns(a), _without_towns(b)
    if (plain_a == a) == (plain_b == b):
        return False
    who_a = {t for t in plain_a.split() if t not in _GENERIC}
    who_b = {t for t in plain_b.split() if t not in _GENERIC}
    return len(who_a) >= 2 and who_a == who_b


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


# A map outline: (south, west, north, east) in degrees.
Box = tuple[float, float, float, float]
Point = tuple[float, float]


@dataclass(frozen=True)
class Site:
    """What says which site a lead (or a saved row) is part of: its site name, its
    phone numbers, where its listings are, their map outlines, and whether it is a
    kind of site that spreads over a mile or more (config.LARGE_SITE_OSM_TAGS)."""

    name: str
    phones: frozenset[str]
    points: tuple[Point, ...]
    boxes: tuple[Box, ...] = ()
    large: bool = False


def outline_box(value: object) -> Box | None:
    """A listing's outline ([south, west, north, east]) as a Box, or None when it has
    none or it is too big to say anything about one site (OUTLINE_MAX_MILES)."""
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        s, w, n, e = (float(x) for x in value)
    except (TypeError, ValueError):
        return None
    if not (s <= n and w <= e) or haversine_miles(s, w, n, e) > OUTLINE_MAX_MILES:
        return None
    return s, w, n, e


def large_site(categories: Iterable[str]) -> bool:
    """True when a listing's map tags say it is an air base, an airport or a campus."""
    for tag in categories:
        if tag in config.LARGE_SITE_TYPES:
            return True
        key, _, value = tag.partition("=")
        if any(key == k and (v is None or v in value.split(";"))
               for k, v in config.LARGE_SITE_OSM_TAGS):
            return True
    return False


def site_of(lead: Lead, parts: list[dict[str, Any]] | None = None) -> Site | None:
    """The lead's Site (parts: the source listings it is made of, default its own
    parts, or the lead itself), or None when its name says nothing about which site
    it is."""
    name = site_name(lead.name)
    if not name:
        return None
    listings = (lead.parts if parts is None else parts) or [
        {"lat": lead.lat, "lon": lead.lon, "outline": lead.outline}]
    points = tuple((float(p["lat"]), float(p["lon"])) for p in listings
                   if p.get("lat") is not None and p.get("lon") is not None)
    boxes = tuple(b for p in listings if (b := outline_box(p.get("outline"))))
    return Site(name, phone_numbers(lead.phone), points or ((lead.lat, lead.lon),), boxes,
                large_site(lead.raw_categories))


def _box(points: list[Point]) -> Box:
    lats, lons = [p[0] for p in points], [p[1] for p in points]
    return min(lats), min(lons), max(lats), max(lons)


def _phones_agree(a: frozenset[str], b: frozenset[str]) -> bool:
    return not (a and b and a.isdisjoint(b))


def _gap(a: Site, b: Site) -> float:
    """Miles between the nearest listings of two sites."""
    return min(haversine_miles(p[0], p[1], q[0], q[1]) for p in a.points for q in b.points)


@lru_cache(maxsize=65536)
def _grown(box: Box) -> Box:
    """The outline with OUTLINE_MARGIN_MILES around it."""
    s, w, n, e = box
    dlat = OUTLINE_MARGIN_MILES / 69.0
    dlon = dlat / max(0.2, math.cos(math.radians((s + n) / 2)))
    return s - dlat, w - dlon, n + dlat, e + dlon


def _within(points: Iterable[Point], boxes: Iterable[Box]) -> bool:
    """True when every point lies inside one of the outlines (with their margin)."""
    grown = [_grown(b) for b in boxes]
    return bool(grown) and all(any(g[0] <= lat <= g[2] and g[1] <= lon <= g[3] for g in grown)
                               for lat, lon in points)


def _inside(a: Site, b: Site) -> bool:
    """A listing of b lies inside an outline of a."""
    return any(_within([p], a.boxes) for p in b.points)


def same_site(a: Site | None, b: Site | None) -> bool:
    """Two parts of one site, with no phone numbers that differ: the same site name and
    a listing of one inside the outline of the other; the same name, both a large site
    (an air base, an airport, a campus) within LARGE_SITE_MILES; the same name, a
    listing of one within SAME_NAME_MILES of a listing of the other and the two together
    at most SITE_MILES across; or names that differ only by a town written into one
    ("Smith's Distribution Center" / "Smith's Layton Distribution") and a listing of one
    inside the outline of the other."""
    if a is None or b is None or not _phones_agree(a.phones, b.phones):
        return False
    if a.name != b.name:
        return town_added(a.name, b.name) and (_inside(a, b) or _inside(b, a))
    if _inside(a, b) or _inside(b, a) or (a.large and b.large and _gap(a, b) <= LARGE_SITE_MILES):
        return True
    if haversine_miles(*_box(list(a.points + b.points))) > SITE_MILES:
        return False
    return _gap(a, b) <= SAME_NAME_MILES


def _held(members: list[Site]) -> bool:
    """A group of sites that can be one site: at most SITE_MILES across, every listing
    inside one of its outlines, or all large sites at most LARGE_SITE_MILES across."""
    points = [p for m in members for p in m.points]
    span = haversine_miles(*_box(points))
    if span <= SITE_MILES:
        return True
    if all(m.large for m in members) and span <= LARGE_SITE_MILES:
        return True
    return _within(points, [b for m in members for b in m.boxes])


def _reach(site: Site) -> Box:
    """The area a site's partners can be in: its listings and outlines, with room for
    the widest rule that can apply (LARGE_SITE_MILES for a large site)."""
    pts = list(site.points) + [(g[0], g[1]) for g in map(_grown, site.boxes)] + \
        [(g[2], g[3]) for g in map(_grown, site.boxes)]
    s, w, n, e = _box(pts)
    dlat = (LARGE_SITE_MILES if site.large else SITE_MILES) / 69.0
    dlon = dlat / max(0.2, math.cos(math.radians((s + n) / 2)))
    return s - dlat, w - dlon, n + dlat, e + dlon


# The grid SiteIndex finds neighbours on, in degrees (about 1.4 miles of latitude).
_CELL = 0.02
Cell = tuple[int, int]


def _cells(box: Box) -> list[Cell]:
    s, w, n, e = box
    return [(i, j) for i in range(math.floor(s / _CELL), math.floor(n / _CELL) + 1)
            for j in range(math.floor(w / _CELL), math.floor(e / _CELL) + 1)]


def _point_cells(site: Site) -> set[Cell]:
    return {(math.floor(lat / _CELL), math.floor(lon / _CELL)) for lat, lon in site.points}


class SiteIndex:
    """Sites by where they are, to find the ones that can be parts of one site with
    another (same_site) without comparing every pair: a partner has a listing within
    the other's reach (_reach), so each site is filed under the grid cells of its
    listings and of its reach, and looked up by both."""

    def __init__(self) -> None:
        self._at: dict[Cell, list[Any]] = {}
        self._reaching: dict[Cell, list[Any]] = {}

    def add(self, key: Any, site: Site) -> None:
        for cell in _point_cells(site):
            self._at.setdefault(cell, []).append(key)
        for cell in _cells(_reach(site)):
            self._reaching.setdefault(cell, []).append(key)

    def near(self, site: Site) -> list[Any]:
        """The keys of the sites that may be parts of one site with this one, each once."""
        keys: dict[Any, None] = {}
        for cell in _cells(_reach(site)):
            keys.update(dict.fromkeys(self._at.get(cell, ())))
        for cell in _point_cells(site):
            keys.update(dict.fromkeys(self._reaching.get(cell, ())))
        return list(keys)


def _site_pairs(sites: list[Site | None]) -> list[tuple[float, int, int]]:
    """Every pair of sites that are parts of one site (same_site), closest first."""
    index = SiteIndex()
    for i, site in enumerate(sites):
        if site is not None:
            index.add(i, site)
    near = {(min(i, j), max(i, j)) for i, a in enumerate(sites) if a is not None
            for j in index.near(a) if j != i}
    return sorted((round(_gap(sites[i], sites[j]), 6), i, j)
                  for i, j in near if same_site(sites[i], sites[j]))


def site_groups(sites: list[Site | None]) -> list[list[int]]:
    """Group the sites that are parts of one site (same_site, closest first), keeping
    every group one site (_held) and free of differing phone numbers. Returns the
    groups of more than one, by index."""
    group: dict[int, list[int]] = {}
    root: dict[int, int] = {}
    for _, i, j in _site_pairs(sites):
        ri, rj = root.get(i, i), root.get(j, j)
        if ri == rj:
            continue
        gi, gj = group.get(ri, [ri]), group.get(rj, [rj])
        members = gi + gj
        if not _held([sites[m] for m in members]):     # type: ignore[misc]
            continue
        if not all(_phones_agree(sites[x].phones, sites[y].phones)   # type: ignore[union-attr]
                   for x in gi for y in gj):
            continue
        group[ri] = members
        group.pop(rj, None)
        for m in members:
            root[m] = ri
    return [sorted(g) for g in group.values() if len(g) > 1]


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
