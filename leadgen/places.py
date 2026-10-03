"""The town and ZIP code near a point, worked out offline from its coordinates.

Many businesses in the free map data have no street address and no city, and chain
names repeat (a dozen Smith's, ten Walmarts), so a row needs at least a town to be
told apart. A lead with coordinates but no city (or no ZIP) gets the nearest town and
ZIP code area from a small table built from the US Census Bureau's Gazetteer files
(data/places.json, see its "about"; rebuilt by `python -m leadgen.build_places`),
with no lookup over the internet. It is an estimate from the map position, so the
pages and downloads show it as "near West Jordan, UT 84088", never as an address;
nothing saved is changed.
"""

import json
import math
import re
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


def biggest_towns(box: tuple[float, float, float, float], centre: tuple[float, float],
                  radius_miles: float, state: str = "UT", most: int = 1) -> list[str]:
    """The biggest (by land area) `most` of the state's towns whose centre is in the box
    (south, west, north, east) and within radius_miles of centre, biggest first (the
    table's order between equals)."""
    towns, _ = _table()
    south, west, north, east = box
    found = [(name.split("|")[0], radius) for name, lat, lon, radius in towns
             if south <= lat <= north and west <= lon <= east and name.endswith(f"|{state}")
             and haversine_miles(centre[0], centre[1], lat, lon) <= radius_miles]
    found.sort(key=lambda town: -town[1])             # stable: equals keep the table's order
    return [name for name, _ in found[:most]]


def biggest_town(box: tuple[float, float, float, float], centre: tuple[float, float],
                 radius_miles: float, state: str = "UT") -> str:
    """The biggest (by land area) of the state's towns whose centre is in the box
    (south, west, north, east) and within radius_miles of centre, or "" when none is:
    how the page names a part of a search's area ("Still filling in, in the background
    until about 1:36 PM: Salt Lake City", osm.areas_text)."""
    found = biggest_towns(box, centre, radius_miles, state)
    return found[0] if found else ""


# ---- a listed city, tidied: the way mappers type a town ("CLEARFIELD", "american Fork",
# "West Jordan City", "Saratoga Spring", "la") shown as the town's own name ("Clearfield",
# "American Fork", "West Jordan", "Saratoga Springs", "Layton"). Only what the pages and
# downloads show changes; the saved rows keep the text exactly as the source gave it.

# Words written short in addresses, and the word each stands for.
_SHORT = {"n": "north", "no": "north", "s": "south", "so": "south", "e": "east", "w": "west",
          "mt": "mount", "ft": "fort", "pt": "point", "hts": "heights", "hgts": "heights",
          "spg": "springs", "spgs": "springs", "spr": "springs", "vly": "valley", "pk": "park",
          "cyn": "canyon", "ctr": "center", "cntr": "centre", "twp": "township", "saint": "st"}
# A state after the town ("Layton, UT", "Ogden Utah") or a county note ("Draper (Sl Co)").
_STATE_NAMES = {"UT": "utah", "ID": "idaho", "NV": "nevada", "WY": "wyoming", "CO": "colorado"}
_AFTER = re.compile(r"\s*\([^)]*\)\s*$|[\s,]+(?:ut|utah|id|idaho|nv|nevada|wy|wyoming|co|colorado)\.?$|,\s*$",
                    re.I)


def _key(text: str) -> str:
    """The words of a town name in one comparable form: lower case, no punctuation, short
    words written out, a plural's "s" dropped ("Saratoga Spring" = "Saratoga Springs")."""
    words = [_SHORT.get(w, w) for w in re.sub(r"[^a-z0-9]+", " ", text.lower()).split()]
    return " ".join(w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words)


@lru_cache(maxsize=8)
def _towns_by_key(state: str) -> tuple[dict[str, str], dict[str, str]]:
    """The state's towns by _key(name), and by their initials when only one town has them
    ("slc" Salt Lake City, "wvc" West Valley City)."""
    towns, _ = _table()
    names = [name.split("|")[0] for name, *_ in towns if name.endswith(f"|{state}")]
    by_key = {_key(n): n for n in names}
    initials: dict[str, list[str]] = {}
    for n in names:
        if len(n.split()) > 1:
            initials.setdefault("".join(w[0] for w in _key(n).split()), []).append(n)
    return by_key, {k: v[0] for k, v in initials.items() if len(v) == 1}


def _readable(text: str) -> str:
    """A town not in the table, readable: "HILL AIR FORCE BASE" / "riverside" in title case
    ("Hill Air Force Base", "Riverside"); mixed case ("McCammon") kept as written."""
    letters = [c for c in text if c.isalpha()]
    if letters and not (all(c.isupper() for c in letters) or all(c.islower() for c in letters)):
        return text
    return re.sub(r"[A-Za-z]+(?:'[A-Za-z]+)?", lambda m: m.group().capitalize(), text)


@lru_cache(maxsize=4096)
def _tidy(city: str, state: str) -> tuple[str, bool]:
    """(the town's name, True) when the table knows it, else (the text made readable,
    False); ("", False) for a city that is only a state."""
    text = " ".join(city.split())
    while (trimmed := _AFTER.sub("", text)) != text:
        text = trimmed
    if text.lower() in {"", *(s.lower() for s in _STATE_NAMES), *_STATE_NAMES.values()}:
        return "", False
    by_key, initials = _towns_by_key(state)
    key = _key(text)
    for k in (key, key.removesuffix(" city"), key + " city"):
        if k in by_key:
            return by_key[k], True
    if " " not in key and key in initials and len(key) >= 3:
        return initials[key], True
    return _readable(text), False


def tidy_town(city: str, state: str = "", lat: float | None = None, lon: float | None = None,
              zip_code: str = "") -> str:
    """A listed city as the town's own name from the table ("CLEARFIELD" -> "Clearfield",
    "W Jordan" / "West Jordan City" -> "West Jordan", "SLC" -> "Salt Lake City"). A
    short form only the town it is near starts with or is the initials of ("la", "AF")
    becomes that town (from the coordinates, else the ZIP code's area); anything else
    unknown stays, made readable ("HILL AIR FORCE BASE" -> "Hill Air Force Base"),
    except one or two letters that name nothing, which say no more than no city ("")."""
    if not city.strip():
        return ""
    state = state.strip().upper() if state.strip().upper() in _STATE_NAMES else "UT"
    name, known = _tidy(city, state)
    if known or not name:
        return name
    short = _key(name).replace(" ", "")
    nearby = _near_town(lat, lon, zip_code, state)
    if nearby and short.isalpha() and len(short) <= 4:
        words = _key(nearby).split()
        if "".join(words).startswith(short) or (len(words) > 1 and "".join(w[0] for w in words) == short):
            return nearby
    return name if len(short) > 2 else ""


def _near_town(lat: float | None, lon: float | None, zip_code: str, state: str) -> str:
    """The table's town nearest the coordinates, else nearest the ZIP code area's centre."""
    found = near(lat, lon)
    if found is None:
        point = _zip_points().get(zip_code.strip()[:5])
        found = near(*point) if point else None
    return found.town if found and found.state == state else ""


@lru_cache(maxsize=1)
def _zip_points() -> dict[str, tuple[float, float]]:
    _, zips = _table()
    return {z: (lat, lon) for z, lat, lon, _ in zips}


def state_code(state: str) -> str:
    """'UT' for "ut", "Utah" or "UT"; any other text as it is."""
    text = state.strip()
    for code, name in _STATE_NAMES.items():
        if text.lower() in (code.lower(), name):
            return code
    return text


def listed_town(lead: Lead) -> str:
    """The lead's own city, tidied (tidy_town); "" when it lists none."""
    return tidy_town(lead.city, lead.state, lead.lat, lead.lon, lead.zip)


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
    if listed_town(lead) and lead.zip.strip():
        return None
    return near(lead.lat, lead.lon)


def town_of(lead: Lead) -> str:
    """The lead's city (tidied), else the town it is near ("" when neither is known)."""
    town = listed_town(lead)
    if town:
        return town
    found = for_lead(lead)
    return found.town if found else ""
