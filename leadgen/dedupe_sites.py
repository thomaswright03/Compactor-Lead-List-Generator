"""The parts of one site: listings of one named place (the buildings of an apartment
complex, a campus, an air base) found apart, by their names, phone numbers, pins and map
outlines (same_site), and grouped without comparing every pair (SiteIndex, site_groups)."""

import math
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from . import config
from .dedupe_names import SAME_NAME_MILES, part_added, phone_numbers, site_name, town_added
from .geo import haversine_miles
from .models import Lead

# One named site spread over many listings (the buildings of an apartment complex,
# the parts of a campus): each listing within SAME_NAME_MILES of the next, the whole
# site at most SITE_MILES across (see dedupe.merge_sites).
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
    ("Smith's Distribution Center" / "Smith's Layton Distribution") or a part of the site
    ("Intermountain Medical Center South Building" / "Intermountain Medical Center") and
    a listing of one inside the outline of the other."""
    if a is None or b is None or not _phones_agree(a.phones, b.phones):
        return False
    if a.name != b.name:
        return ((town_added(a.name, b.name) or part_added(a.name, b.name))
                and (_inside(a, b) or _inside(b, a)))
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
