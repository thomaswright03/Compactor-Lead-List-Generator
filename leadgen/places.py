"""The town and ZIP code near a point, worked out offline from its coordinates.

Many businesses in the free map data have no street address and no city, and chain
names repeat (a dozen Smith's, ten Walmarts), so a row needs at least a town to be
told apart. A lead with coordinates but no city (or no ZIP) gets the nearest town and
ZIP code area from a small table built from the US Census Bureau's Gazetteer files
(data/places.json, see its "about"), with no lookup over the internet. It is an
estimate from the map position, so the pages and downloads show it as "near West
Jordan, UT 84088", never as an address; nothing saved is changed.
"""

import json
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .geo import haversine_miles
from .models import Lead

DATA = Path(__file__).resolve().parent / "data" / "places.json"
# A point farther than this beyond the edge of every town (or ZIP area) in the table
# gets none: "near" a town 40 miles away would mislead more than it helps.
MAX_MILES = 15.0


@dataclass(frozen=True)
class Near:
    """The town (and its state) and the ZIP code area nearest a point; zip may be ""."""

    town: str
    state: str
    zip: str

    def text(self) -> str:
        """'near West Jordan, UT 84088'."""
        return f"near {self.town}, {self.state}" + (f" {self.zip}" if self.zip else "")


# (name, lat, lon, radius in miles) rows for the towns (name "Town|ST") and ZIP areas.
Rows = list[tuple[str, float, float, float]]


@lru_cache(maxsize=1)
def _table() -> tuple[Rows, Rows]:
    data = json.loads(DATA.read_text())
    towns = [(f"{name}|{state}", lat, lon, r) for name, state, lat, lon, r in data["places"]]
    zips = [(z, lat, lon, r) for z, lat, lon, r in data["zips"]]
    return towns, zips


def _closest(rows: Rows, lat: float, lon: float) -> str:
    """The row whose area is closest to the point: the distance to its centre less its
    radius, so a point on the edge of a big city is in that city, not in the small town
    whose centre happens to be a little nearer. "" when none is within MAX_MILES."""
    best, best_gap = "", MAX_MILES
    # A cheap box test first: a degree of latitude is about 69 miles.
    reach = (MAX_MILES + 30) / 69.0
    wide = reach / max(0.2, math.cos(math.radians(lat)))
    for name, plat, plon, radius in rows:
        if abs(plat - lat) > reach or abs(plon - lon) > wide:
            continue
        gap = haversine_miles(lat, lon, plat, plon) - radius
        if gap < best_gap:
            best, best_gap = name, gap
    return best


def town_names(state: str = "UT") -> list[str]:
    """The names of the state's towns in the table ("Layton", "West Jordan"), for telling
    a business's name from the town written into it (dedupe.py)."""
    towns, _ = _table()
    return [name.split("|")[0] for name, *_ in towns if name.endswith(f"|{state}")]


def biggest_town(box: tuple[float, float, float, float], centre: tuple[float, float],
                 radius_miles: float, state: str = "UT") -> str:
    """The biggest (by land area) of the state's towns whose centre is in the box
    (south, west, north, east) and within radius_miles of centre, or "" when none is:
    how the page names a part of a search's area ("Still filling in: around Salt Lake
    City", osm.areas_text)."""
    towns, _ = _table()
    south, west, north, east = box
    best, size = "", -1.0
    for name, lat, lon, radius in towns:
        if not (south <= lat <= north and west <= lon <= east and name.endswith(f"|{state}")):
            continue
        if radius > size and haversine_miles(centre[0], centre[1], lat, lon) <= radius_miles:
            best, size = name.split("|")[0], radius
    return best


@lru_cache(maxsize=20000)
def _near(lat: float, lon: float) -> Near | None:
    towns, zips = _table()
    town = _closest(towns, lat, lon)
    if not town:
        return None
    name, state = town.split("|")
    return Near(name, state, _closest(zips, lat, lon))


def near(lat: float | None, lon: float | None) -> Near | None:
    """The town and ZIP area nearest (lat, lon), or None (no coordinates, or nowhere
    near the towns in the table)."""
    if lat is None or lon is None or not (math.isfinite(lat) and math.isfinite(lon)):
        return None
    if lat == 0 and lon == 0:
        return None
    return _near(round(lat, 4), round(lon, 4))


def for_lead(lead: Lead) -> Near | None:
    """What a lead without a city (or ZIP) is near; None when the listing has both."""
    if lead.city.strip() and lead.zip.strip():
        return None
    return near(lead.lat, lead.lon)


def town_of(lead: Lead) -> str:
    """The lead's city, else the town it is near ("" when neither is known)."""
    if lead.city.strip():
        return lead.city
    found = for_lead(lead)
    return found.town if found else ""
