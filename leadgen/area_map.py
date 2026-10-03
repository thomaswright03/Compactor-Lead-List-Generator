"""What the Map page shows: AARCO's area, the areas a search asks the free map data in,
how the latest search of the area covered them, and a pin for every saved lead.

AARCO's area is the circle of config.SERVICE_AREA_MILES around its shop
(config.SERVICE_CENTER). A search of it asks the free map data in parts
(osm.area_boxes: the "N of 9 areas" of its progress and history); each is drawn as
its box cut to the circle and named like the page names it elsewhere (its biggest
town, osm.area_name). Each area is shaded by the latest search of AARCO's area that
asked the map data, from what the day's record says about it: every area answered,
the parts that didn't (web/finding.py map_areas, and while they are filled in the
fill-in's "boxes", fillin.py), or, for a search recorded before those were kept, the
towns its missing areas were named by. Nothing here writes anything.
"""

import json
import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from . import config, daily, places
from .export import format_phone
from .geo import haversine_miles
from .localtime import date_time_text
from .models import Lead
from .pipeline import EXEMPT_TYPES
from .saved import is_closed
from .sources import osm

# A point as the page gets it: [lat, lon], rounded to about a metre.
Point = list[float]
Box = tuple[float, float, float, float]

# The points of the drawn circle (and of each area's curved edge).
CIRCLE_POINTS = 180
# A search counts as one of AARCO's area when it ran this close to the shop.
SAME_CENTRE_MILES = 0.1
# An area this much (or more) of which was missed reads as missed, not partly; this little
# (the edge of a neighbouring part) as searched.
WHOLE, SLIVER = 0.99, 0.01

# How an area stands after the latest search of AARCO's area, and the page's words.
SEARCHED, PARTLY, MISSED, ASKING, UNKNOWN = "searched", "partly", "missed", "asking", "unknown"
STATUS_TEXT = {SEARCHED: "Searched", PARTLY: "Partly searched: some of it didn't come in",
               MISSED: "Not searched: the free map data didn't come in for it",
               ASKING: "Not searched yet: still filling in, in the background",
               UNKNOWN: "Not known"}
# PARTLY while the missing parts are still being asked.
PARTLY_FILLING = "Partly searched: the rest is still filling in, in the background"

# The pins' groups: the Yes / No answer, or a competitor (and AARCO's own listing).
GROUPS = ("unchecked", "yes", "no", "competitor")
# A pin's fields, in the order the page gets them (one short list per lead).
FIELDS = ("key", "lat", "lon", "group", "name", "tier", "score", "phone", "verified_phone",
          "address", "outcome", "called", "closed", "kind")


@dataclass(frozen=True)
class Area:
    n: int                     # its place in the order the search asks them (1 = the centre's)
    name: str                  # "Salt Lake City", "The area to the north-west"
    towns: tuple[str, ...]     # its biggest towns after the one it is named for
    box: Box
    outline: tuple[tuple[float, float], ...]   # the box cut to the circle
    label: tuple[float, float]                 # where its name is written


def _circle(lat: float, lon: float, miles: float, points: int = CIRCLE_POINTS) -> list[tuple[float, float]]:
    """The circle of `miles` around (lat, lon) as points, on the same flat reckoning as
    the search's areas (osm._square), so the two meet exactly."""
    dlat = miles / osm.MILES_PER_DEGREE_LAT
    dlon = dlat / max(0.01, math.cos(math.radians(lat)))
    return [(lat + dlat * math.cos(2 * math.pi * i / points), lon + dlon * math.sin(2 * math.pi * i / points))
            for i in range(points)]


def _clip(polygon: Sequence[tuple[float, float]], box: Box) -> list[tuple[float, float]]:
    """The part of a convex polygon inside the box (Sutherland-Hodgman)."""
    south, west, north, east = box
    edges = ((0, south, True), (0, north, False), (1, west, True), (1, east, False))
    points = list(polygon)
    for axis, limit, above in edges:
        if not points:
            break

        def inside(p: tuple[float, float], axis: int = axis, limit: float = limit, above: bool = above) -> bool:
            return p[axis] >= limit if above else p[axis] <= limit

        def cross(a: tuple[float, float], b: tuple[float, float], axis: int = axis,
                  limit: float = limit) -> tuple[float, float]:
            t = (limit - a[axis]) / (b[axis] - a[axis])
            return (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))

        out: list[tuple[float, float]] = []
        for i, here in enumerate(points):
            before = points[i - 1]
            if inside(here):
                if not inside(before):
                    out.append(cross(before, here))
                out.append(here)
            elif inside(before):
                out.append(cross(before, here))
        points = out
    return points


def _centroid(polygon: Sequence[tuple[float, float]]) -> tuple[float, float]:
    """The polygon's centre of area (where its label goes)."""
    area = lat = lon = 0.0
    for i, (y1, x1) in enumerate(polygon):
        y0, x0 = polygon[i - 1]
        step = x0 * y1 - x1 * y0
        area += step
        lon += (x0 + x1) * step
        lat += (y0 + y1) * step
    if abs(area) < 1e-12:
        return (sum(p[0] for p in polygon) / len(polygon), sum(p[1] for p in polygon) / len(polygon))
    return (lat / (3 * area), lon / (3 * area))


def _area_title(name: str) -> str:
    return name[:1].upper() + name[1:]


@lru_cache(maxsize=4)
def areas(lat: float = config.SERVICE_CENTER[0], lon: float = config.SERVICE_CENTER[1],
          miles: float = config.SERVICE_AREA_MILES) -> tuple[Area, ...]:
    """The areas a search of `miles` around (lat, lon) asks the map data in (by default
    AARCO's area), nearest the centre first, each cut to the circle and named."""
    circle = _circle(lat, lon, miles)
    found = []
    for n, box in enumerate(osm.area_boxes(lat, lon, miles), 1):
        outline = _clip(circle, box)
        if len(outline) < 3:
            continue
        towns = places.biggest_towns(box, (lat, lon), miles, most=4)
        name = osm.area_name(lat, lon, miles, box)
        label = _centroid(outline)
        south, west, north, east = box
        if south < lat < north and west < lon < east:
            # The centre's own pin (AARCO's) stands there: the name goes below it.
            label = (lat - (north - south) / 4, lon)
        found.append(Area(n, _area_title(name), tuple(t for t in towns if t != name)[:3], box,
                          tuple(outline), label))
    return tuple(found)


def _point(p: tuple[float, float]) -> Point:
    return [round(p[0], 5), round(p[1], 5)]


# ---- how the latest search of AARCO's area covered each area

def _of_aarcos_area(record: dict[str, Any]) -> bool:
    """True for a search that found leads, ran around AARCO's shop with the service
    area's miles, and asked the free map data: the areas drawn are its own."""
    if "leads" not in record or record.get("source", "auto") not in ("auto", "osm", "both"):
        return False
    try:
        if float(record.get("radius") or 0) != config.SERVICE_AREA_MILES:
            return False
    except (TypeError, ValueError):
        return False
    centre = record.get("center")
    if isinstance(centre, list) and len(centre) == 2:
        try:
            return haversine_miles(float(centre[0]), float(centre[1]), *config.SERVICE_CENTER) <= SAME_CENTRE_MILES
        except (TypeError, ValueError):
            return False
    miles = record.get("miles")              # recorded before the centre was kept
    return isinstance(miles, int | float) and miles <= SAME_CENTRE_MILES


def latest_search(limit: int = 60) -> dict[str, Any] | None:
    """The latest search of AARCO's area that found leads and asked the map data (the
    day's record, as the search history reads it), or None."""
    return next((r for r in daily.records(limit) if _of_aarcos_area(r)), None)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _missed_share(box: Box, missing: Iterable[Box]) -> float:
    """How much of an area the missing parts make up (0 to 1)."""
    south, west, north, east = box
    whole = (north - south) * (east - west)
    if whole <= 0:
        return 0.0
    share = 0.0
    for s, w, n, e in missing:
        height, width = min(north, n) - max(south, s), min(east, e) - max(west, w)
        if height > 0 and width > 0:
            share += height * width / whole
    return min(1.0, share)


def _missing_boxes(record: dict[str, Any]) -> list[Box] | None:
    """The parts of the area still missing after the search (and its filling in), or
    None when the record doesn't say which they are."""
    fill = _dict(record.get("fill"))
    kept = fill.get("boxes")
    if kept is None:
        kept = _dict(record.get("map_areas")).get("missing")
    if kept is None and not record.get("partial") and not fill:
        return []                            # every source answered in full
    if not isinstance(kept, list):
        return None
    boxes: list[Box] = []
    for box in kept:
        if box is None:                      # the whole circle, asked as one part
            boxes.append((-90.0, -180.0, 90.0, 180.0))
        elif isinstance(box, list) and len(box) == 4:
            boxes.append((float(box[0]), float(box[1]), float(box[2]), float(box[3])))
    return boxes


# An incomplete search whose map data didn't answer at all ("Couldn't reach Google and
# the map data service, so ...").
_MAP_DATA_DOWN = re.compile(r"Couldn't reach [^,]*the map data service")


# What an incomplete search's reason said about the map data before the missing parts were
# kept: "(about 7 of 9 areas searched; not yet: Layton and Coalville)".
_NOT_YET = re.compile(r"about (\d+) of (\d+) areas searched; not yet: ([^)]*)\)")


def _named_missing(record: dict[str, Any]) -> tuple[set[str], bool] | None:
    """For a search recorded before the missing parts were kept: the names of the areas
    the record says were missing (the towns they hold, nearest first), and whether they
    are surely the missing areas' own names (every name listed, as many as the areas
    missing); None when the record doesn't say."""
    fill = _dict(record.get("fill"))
    if fill.get("state") == "complete" or (fill and fill.get("left") == 0):
        return set(), True
    if not fill and "osm areas searched" not in _dict(record.get("details")):
        return set(), True                   # incomplete for another source: the map data answered
    where, left = str(fill.get("where") or ""), fill.get("left")
    said = _NOT_YET.search(str(record.get("reason") or ""))
    if not where and said:
        where, left = said.group(3), int(said.group(2)) - int(said.group(1))
    if not where:
        return None
    names = {_area_title(n.strip()) for chunk in where.removesuffix(" and more").split(", ")
             for n in chunk.split(" and ") if n.strip()}
    return names, not where.endswith(" and more") and len(names) == left


@dataclass
class Coverage:
    statuses: list[str]          # each area's (SEARCHED, PARTLY, MISSED, ASKING or UNKNOWN)
    note: str                    # which search it is, and what it covered, for the page
    filling: bool = False        # its missing areas are still being asked in the background


def coverage(record: dict[str, Any] | None, drawn: Sequence[Area]) -> Coverage:
    """How the search on record (latest_search) covered each of the areas drawn."""
    unknown = [UNKNOWN] * len(drawn)
    if record is None:
        return Coverage(unknown, "No search of AARCO's area has been recorded yet, so the areas aren't shaded.")
    about = f"Shaded by the latest search of AARCO's area ({record.get('when') or record.get('day')})"
    fill = _dict(record.get("fill"))
    filling = fill.get("state") == "filling"
    missed = ASKING if filling else MISSED
    map_areas = _dict(record.get("map_areas"))
    if record.get("stopped") or map_areas.get("known") is False:
        return Coverage(unknown, f"{about}: it was stopped partway by the administrator, so which areas it "
                                 "reached isn't known.")
    missing = _missing_boxes(record)
    if missing is None and _MAP_DATA_DOWN.search(str(record.get("reason") or "")):
        missing = [(-90.0, -180.0, 90.0, 180.0)]
    if missing is not None:
        shares = [_missed_share(a.box, missing) for a in drawn]
        statuses = [SEARCHED if s <= SLIVER else missed if s >= WHOLE else PARTLY for s in shares]
    else:
        named = _named_missing(record)
        if named is None:
            return Coverage(unknown, f"{about}: some areas didn't answer, and which ones wasn't recorded "
                                     "then. The next search records each area.", filling)
        names, listed = named
        hit = [a.name in names for a in drawn]
        # The names are the missing areas' own only when each is an area's; otherwise a
        # quarter of another area (named by its own biggest town) may be missing too.
        sure = (listed and sum(hit) == len(names)) or not names
        statuses = [missed if h else SEARCHED if sure else UNKNOWN for h in hit]
        if not sure:
            return Coverage(statuses, f"{about}: the areas it names as missing are marked; whether the others "
                                      "were searched in full wasn't recorded then.", filling)
    searched = statuses.count(SEARCHED)
    if searched == len(drawn):
        return Coverage(statuses, f"{about}: all {len(drawn)} areas searched.", filling)
    still = ", the rest still being asked in the background" if filling else ""
    return Coverage(statuses, f"{about}: {searched} of {len(drawn)} areas searched in full{still}.", filling)


# ---- the pins

def group(lead: Lead) -> str:
    """The pin's group: a competitor (or AARCO's own listing), else its Yes / No answer."""
    if lead.lead_type in EXEMPT_TYPES:
        return "competitor"
    return lead.has_baler if lead.has_baler in ("yes", "no") else "unchecked"


def _has_position(lead: Lead) -> bool:
    try:
        lat, lon = float(lead.lat), float(lead.lon)
    except (TypeError, ValueError):
        return False
    return math.isfinite(lat) and math.isfinite(lon) and not (lat == 0 and lon == 0) \
        and -90 <= lat <= 90 and -180 <= lon <= 180


def _address(lead: Lead) -> str:
    """Its address as the Leads page writes it: "No street address · near West Jordan,
    UT 84088" for a listing without one."""
    city = places.listed_town(lead)
    near = places.for_lead(lead) if not city else None
    words = [] if lead.address else ["No street address"]
    place = ", ".join(x for x in (lead.address, city, lead.zip) if x)
    if place:
        words.append(place)
    if near:
        words.append(near.text())
    return " · ".join(words)


def pin(lead: Lead) -> list[Any]:
    """One lead as the page gets it (FIELDS, in order)."""
    return [lead.uid, round(float(lead.lat), 5), round(float(lead.lon), 5), group(lead), lead.name,
            lead.tier, lead.score, format_phone(lead.phone), format_phone(lead.verified_phone),
            _address(lead), lead.call_outcome, date_time_text(lead.last_call_at) if lead.call_outcome else "",
            is_closed(lead), lead.lead_type if lead.lead_type in EXEMPT_TYPES else ""]


def page(leads: list[Lead], record: dict[str, Any] | None, unread: bool = False) -> dict[str, Any]:
    """Everything the Map page draws: AARCO's pin, the circle, the areas and how the
    latest search of the area covered them (`unread`: the searches couldn't be read),
    and a pin for every saved lead that has a map position (with how many have none)."""
    lat, lon = config.SERVICE_CENTER
    drawn = areas()
    covered = coverage(record, drawn)
    if unread:
        covered.note = "The search history couldn't be read just now, so the areas aren't shaded."
    shown = [l for l in leads if _has_position(l)]
    counts = dict.fromkeys(GROUPS, 0)
    for lead in shown:
        counts[group(lead)] += 1
    return {
        "aarco": {"name": "AARCO", "company": config.OWN_COMPANY, "address": config.OWN_ADDRESS,
                  "lat": config.OWN_COORDS[0], "lon": config.OWN_COORDS[1]},
        "center": [lat, lon], "miles": config.SERVICE_AREA_MILES,
        "circle": [_point(p) for p in _circle(lat, lon, config.SERVICE_AREA_MILES)],
        "areas": [{"n": a.n, "name": a.name, "towns": list(a.towns), "outline": [_point(p) for p in a.outline],
                   "label": _point(a.label), "status": status,
                   "status_text": PARTLY_FILLING if status == PARTLY and covered.filling else STATUS_TEXT[status]}
                  for a, status in zip(drawn, covered.statuses)],
        "coverage": covered.note,
        "fields": list(FIELDS),
        "pins": [pin(l) for l in shown],
        "counts": counts,
        "no_position": len(leads) - len(shown),
    }


def dumps(body: dict[str, Any]) -> str:
    """The page's answer, compact (a few thousand pins stay small)."""
    return json.dumps(body, separators=(",", ":"))
