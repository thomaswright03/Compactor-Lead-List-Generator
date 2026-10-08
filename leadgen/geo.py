"""Turn a ZIP / city / "lat,lon" into coordinates and measure distances."""

import logging
import math
import os
import re
from typing import Any, Self

from . import config
from .http import HttpError, request_json

log = logging.getLogger(__name__)

EARTH_RADIUS_MILES = 3958.8
METERS_PER_MILE = 1609.344

# Works offline for AARCO's shop and the default area (the label is what the page shows).
KNOWN_PLACES = {
    config.OWN_ADDRESS.lower(): config.OWN_COORDS,
    "876 fortune rd": config.OWN_COORDS,
    "876 fortune road": config.OWN_COORDS,
    "876 fortune rd, salt lake city": config.OWN_COORDS,
    "876 fortune rd, salt lake city, ut": config.OWN_COORDS,
    "876 fortune road, salt lake city": config.OWN_COORDS,
    "876 fortune road, salt lake city, ut 84104": config.OWN_COORDS,
    "1876 w fortune rd, salt lake city, ut 84104": config.OWN_COORDS,
    "aarco": config.OWN_COORDS,
    "aarco compactor": config.OWN_COORDS,
    "aarco compactors & balers": config.OWN_COORDS,
    config.OWN_COMPANY.lower(): config.OWN_COORDS,
    # The spelling the site used before the owner corrected it (AARCO), still understood.
    "arco": config.OWN_COORDS,
    "arco compactor": config.OWN_COORDS,
    "slc": (40.7608, -111.8910),
}
OWN_LABEL = f"{config.OWN_COMPANY}, {config.OWN_ADDRESS}"

# Cities and towns of the Salt Lake area (and nearby): typed bare ("Murray"), with
# ", UT" or with ", Utah", they mean the Utah place, never a same-named one elsewhere
# (Murray, Georgia). They work offline.
UTAH_PLACES = {
    "Salt Lake City": (40.7608, -111.8910), "South Salt Lake": (40.7188, -111.8883),
    "North Salt Lake": (40.8486, -111.9069), "West Valley City": (40.6916, -112.0011),
    "West Valley": (40.6916, -112.0011), "West Jordan": (40.6097, -111.9391),
    "South Jordan": (40.5622, -111.9297), "Sandy": (40.5649, -111.8389),
    "Draper": (40.5247, -111.8638), "Murray": (40.6669, -111.8880),
    "Midvale": (40.6111, -111.8999), "Cottonwood Heights": (40.6197, -111.8102),
    "Holladay": (40.6688, -111.8247), "Millcreek": (40.6866, -111.8755),
    "Taylorsville": (40.6677, -111.9388), "Kearns": (40.6600, -111.9963),
    "Magna": (40.7091, -112.1016), "Riverton": (40.5219, -111.9391),
    "Herriman": (40.5141, -112.0330), "Bluffdale": (40.4897, -111.9388),
    "Bountiful": (40.8894, -111.8808), "Woods Cross": (40.8716, -111.8922),
    "Centerville": (40.9180, -111.8722), "Farmington": (40.9805, -111.8874),
    "Kaysville": (41.0352, -111.9386), "Layton": (41.0602, -111.9711),
    "Clearfield": (41.1108, -112.0261), "Syracuse": (41.0894, -112.0647),
    "Ogden": (41.2230, -111.9738), "Tooele": (40.5308, -112.2983),
    "Stansbury Park": (40.6377, -112.2961), "Lehi": (40.3916, -111.8508),
    "American Fork": (40.3769, -111.7958), "Pleasant Grove": (40.3641, -111.7385),
    "Saratoga Springs": (40.3491, -111.9047), "Eagle Mountain": (40.3141, -112.0069),
    "Orem": (40.2969, -111.6946), "Provo": (40.2338, -111.6585),
    "Park City": (40.6461, -111.4980),
}
# Utah's bounding box (west, north, east, south), which online lookups prefer.
UTAH_BOX = (-114.06, 42.01, -109.04, 36.99)
_STATES = {
    "al": "alabama", "ak": "alaska", "az": "arizona", "ar": "arkansas", "ca": "california",
    "co": "colorado", "ct": "connecticut", "de": "delaware", "fl": "florida", "ga": "georgia",
    "hi": "hawaii", "id": "idaho", "il": "illinois", "in": "indiana", "ia": "iowa",
    "ks": "kansas", "ky": "kentucky", "la": "louisiana", "me": "maine", "md": "maryland",
    "ma": "massachusetts", "mi": "michigan", "mn": "minnesota", "ms": "mississippi",
    "mo": "missouri", "mt": "montana", "ne": "nebraska", "nv": "nevada",
    "nh": "new hampshire", "nj": "new jersey", "nm": "new mexico", "ny": "new york",
    "nc": "north carolina", "nd": "north dakota", "oh": "ohio", "ok": "oklahoma",
    "or": "oregon", "pa": "pennsylvania", "ri": "rhode island", "sc": "south carolina",
    "sd": "south dakota", "tn": "tennessee", "tx": "texas", "vt": "vermont",
    "va": "virginia", "wa": "washington", "wv": "west virginia", "wi": "wisconsin",
    "wy": "wyoming", "dc": "district of columbia",
}


class GeocodeError(ValueError):
    pass


class LookupDown(GeocodeError):
    """No place lookup answered (each one failed or timed out): the place may well
    exist, it just couldn't be looked up now."""


# The longest part of what was typed an error message repeats.
ECHO_CHARS = 50


def shown(text: str) -> str:
    """What was typed, as an error message repeats it: at most ECHO_CHARS characters."""
    text = " ".join(text.split())
    return text if len(text) <= ECHO_CHARS else text[:ECHO_CHARS].rstrip() + "…"


def _place_key(text: str) -> str:
    """'876 Fortune Rd,  Salt Lake City, UT' -> '876 fortune rd salt lake city ut'."""
    return " ".join(re.sub(r"[,.]", " ", text.lower()).split())


def _known_places() -> dict[str, tuple[float, float, str]]:
    known = {_place_key(k): (v[0], v[1], OWN_LABEL if v == config.OWN_COORDS
                             else "Salt Lake City, UT") for k, v in KNOWN_PLACES.items()}
    for name, (lat, lon) in UTAH_PLACES.items():
        for typed in (name, f"{name}, UT", f"{name}, Utah"):
            known.setdefault(_place_key(typed), (lat, lon, f"{name}, UT"))
    return known


_KNOWN = _known_places()


def names_another_state(text: str) -> bool:
    """True when the place typed names a US state other than Utah ("Portland, OR",
    "Murray, Kentucky", "Austin TX 78701"); then it isn't looked for in Utah first."""
    words = _place_key(re.sub(r"\b\d{5}(?:-\d{4})?\b", " ", text))
    if not words:
        return False
    tail = text.rsplit(",", 1)[1] if "," in text else ""
    tail = _place_key(re.sub(r"\b\d{5}(?:-\d{4})?\b", " ", tail))
    names = set(_STATES.values())
    if tail and (tail in _STATES or tail in names):
        return True
    return any(words == n or words.endswith(" " + n) for n in names)


def miles_from_aarco(lat: float, lon: float) -> float:
    """How far a point is from the centre of AARCO's area (config.SERVICE_CENTER)."""
    return haversine_miles(config.SERVICE_CENTER[0], config.SERVICE_CENTER[1], lat, lon)


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(a))


def offset_point(lat: float, lon: float, distance_miles: float, bearing_deg: float) -> tuple[float, float]:
    """Point reached by travelling distance_miles from (lat, lon) on a bearing."""
    d = distance_miles / EARTH_RADIUS_MILES
    b = math.radians(bearing_deg)
    p1, l1 = math.radians(lat), math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
    l2 = l1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(p1),
                         math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), math.degrees(l2)


def search_grid(lat: float, lon: float, radius_miles: float, cells: int) -> list[tuple[float, float, float]]:
    """Cover a big circle with smaller overlapping circles.

    Google returns at most 60 results per query, so splitting a 30-mile area
    into 7 or 19 cells finds far more businesses. Returns (lat, lon, radius).
    """
    if cells <= 1:
        return [(lat, lon, radius_miles)]
    if cells <= 7:
        ring_r = radius_miles * 0.6
        sub_r = radius_miles * 0.5
        pts = [(lat, lon)] + [offset_point(lat, lon, ring_r, b) for b in range(0, 360, 60)]
        return [(a, b, sub_r) for a, b in pts]
    sub_r = radius_miles * 0.3
    pts = [(lat, lon)]
    pts += [offset_point(lat, lon, radius_miles * 0.4, b) for b in range(0, 360, 60)]
    pts += [offset_point(lat, lon, radius_miles * 0.78, b) for b in range(0, 360, 30)]
    return [(a, b, sub_r) for a, b in pts]


_LATLON = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")
_ZIP = re.compile(r"^\s*(\d{5})(?:-\d{4})?\s*$")


# A place found: (lat, lon, the label the page shows for it).
Found = tuple[float, float, str]
# What one lookup gave: the place, or None, and whether the service answered at all (even
# "no such place"), so a place nobody could find is told from an outage.
Lookup = tuple[Found | None, bool]


class Place(tuple[float, float, str]):
    """A place an online lookup found: (lat, lon, label), like Found, and `doubt`: why it
    may not be the place meant, in the page's words ("We found a road called Springfield
    Road in Salt Lake City. Is that what you meant?"), or "" when it is a town, a ZIP
    code or a street address. The page asks before a search runs around a doubtful
    place (web/place.py)."""

    doubt: str

    def __new__(cls, lat: float, lon: float, label: str, doubt: str = "") -> Self:
        place = super().__new__(cls, (lat, lon, label))
        place.doubt = doubt
        return place


# Some letter (in any alphabet): text without one (a date, a phone number, a bare number
# that isn't a ZIP code) is not a place, so it is never looked up.
_LETTER = re.compile(r"[^\W\d_]")
# A street address typed: it starts with a house number ("4800 S State St").
_HOUSE_NUMBER = re.compile(r"^\s*\d+[a-z]?\s+\S", re.I)

# The free lookup's (OpenStreetMap's) matches that are a town, a ZIP code or a street
# address, by class and type; any other (a road, a trail, a park, a business) may not
# be the place meant.
_OSM_TOWNS = {"city", "town", "village", "hamlet", "suburb", "neighbourhood", "quarter", "borough",
              "municipality", "city_block", "county", "state", "postcode", "house"}
_OSM_BOUNDARIES = {"administrative", "postal_code", "census"}
_OSM_TRAILS = {"path", "footway", "track", "bridleway", "cycleway", "steps", "pedestrian"}
# How the page names the other matches: by class and type, else by class.
_OSM_KINDS = {("leisure", "park"): "a park", ("boundary", "national_park"): "a park",
              ("boundary", "protected_area"): "a protected area", ("natural", "peak"): "a mountain peak",
              ("natural", "water"): "a lake or pond", ("aeroway", "aerodrome"): "an airport",
              ("place", "locality"): "a named spot", ("place", "farm"): "a farm",
              ("place", "isolated_dwelling"): "a lone house", ("place", "square"): "a square",
              ("place", "island"): "an island"}
_OSM_CLASSES = {"railway": "a railway", "waterway": "a river or canal", "natural": "a natural feature",
                "leisure": "a park or sports ground", "landuse": "an area of land", "aeroway": "an airport",
                "building": "a building", "amenity": "a business or building", "shop": "a business",
                "office": "a business", "craft": "a business", "tourism": "a tourist spot",
                "historic": "a historic site", "man_made": "a structure"}
# Google's: the result types that are a town, a ZIP code or a street address (and the
# ones starting with these), and how the page names a few others.
_GOOGLE_TOWNS = {"locality", "neighborhood", "postal_town", "colloquial_area", "street_address", "premise",
                 "subpremise"}
_GOOGLE_TOWN_PREFIXES = ("administrative_area_level", "sublocality", "postal_code")
_GOOGLE_KINDS = (("route", "a road"), ("intersection", "a street corner"), ("park", "a park"),
                 ("airport", "an airport"), ("natural_feature", "a natural feature"),
                 ("establishment", "a business or building"), ("point_of_interest", "a business or building"))


def _feature_doubt(kind: str, name: str, town: str, state: str, location: str) -> str:
    """The page's question about a match that is only `kind` ("a road") named `name`."""
    if town and state and state != "Utah":
        town = f"{town}, {state}"
    called = f" called {name}" if name else ""
    near = f" in {town}" if town and town != name else ""
    street = ", but not the street number typed" if kind == "a road" and _HOUSE_NUMBER.match(location) else ""
    return f"We found {kind}{called}{near}{street}. Is that what you meant?"


def _osm_doubt(match: dict[str, Any], location: str) -> str:
    """Why the free lookup's match may not be the place meant (Place.doubt), from its own
    class and type; "" for a town, a ZIP code or a street address, or when it doesn't say."""
    cls, kind = str(match.get("class") or ""), str(match.get("type") or "")
    raw = match.get("address")
    address: dict[str, Any] = raw if isinstance(raw, dict) else {}
    if (not cls or kind == "postcode" or (cls == "place" and (not kind or kind in _OSM_TOWNS))
            or (cls == "boundary" and (not kind or kind in _OSM_BOUNDARIES))):
        return ""
    if address.get("house_number") and _HOUSE_NUMBER.match(location):
        return ""                 # the street address typed (a building or a business at it)
    if cls == "highway":
        what = "a trail" if kind in _OSM_TRAILS else "a road"
    else:
        what = _OSM_KINDS.get((cls, kind)) or _OSM_CLASSES.get(cls, "a map feature")
    name = str(match.get("name") or "") or str(match.get("display_name") or "").split(",")[0].strip()
    town = next((str(address[k]) for k in ("city", "town", "village", "hamlet", "suburb", "county")
                 if address.get(k)), "")
    return _feature_doubt(what, name, town, str(address.get("state") or ""), location)


def _google_doubt(result: dict[str, Any], location: str) -> str:
    """The same for Google's match, from its result types."""
    types = {str(t) for t in result.get("types") or []}
    if not types or types & _GOOGLE_TOWNS or any(t.startswith(_GOOGLE_TOWN_PREFIXES) for t in types):
        return ""
    what = next((words for t, words in _GOOGLE_KINDS if t in types), "a map feature")
    parts = [p for p in result.get("address_components") or [] if isinstance(p, dict)]

    def part(kind: str) -> str:
        return next((str(p.get("long_name") or "") for p in parts if kind in (p.get("types") or [])), "")
    name = str(parts[0].get("long_name") or "") if parts else ""
    town = part("locality") or part("administrative_area_level_2")
    return _feature_doubt(what, name, town, part("administrative_area_level_1"), location)


def _coordinates(location: str) -> Found | None:
    """The place typed as "lat,lon", checked to be on the globe; None for anything else."""
    m = _LATLON.match(location)
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise GeocodeError(f"The coordinates {shown(location)} are out of range: latitude "
                           "must be between -90 and 90, longitude between -180 and 180.")
    return lat, lon, location


def _zip_lookup(code: str, errors: list[str]) -> Lookup:
    """A US ZIP code's town and position (the free zippopotam.us lookup)."""
    try:
        data = request_json("GET", f"https://api.zippopotam.us/us/{code}", retries=2)
    except HttpError as exc:
        errors.append(str(exc))
        return None, exc.status == 404                   # an unknown ZIP code
    try:
        place = data["places"][0]
        label = f"{place['place name']}, {place['state abbreviation']} {code}"
        return Place(float(place["latitude"]), float(place["longitude"]), label), True
    except (KeyError, IndexError, ValueError) as exc:
        errors.append(str(exc))
        return None, True


def _google_lookup(location: str, api_key: str, elsewhere: bool, errors: list[str]) -> Lookup:
    """Google's geocoder, preferring Utah unless the place names another state."""
    try:
        params = {"address": location, "key": api_key, "components": "country:US"}
        if not elsewhere:
            # Prefer Utah: Google returns the Utah place for an ambiguous name.
            west, north, east, south = UTAH_BOX
            params["bounds"] = f"{south},{west}|{north},{east}"
        data = request_json("GET", "https://maps.googleapis.com/maps/api/geocode/json",
                            params=params, retries=2,
                            cache_key_extra="google",
                            cacheable=lambda v: isinstance(v, dict)
                            and v.get("status") in ("OK", "ZERO_RESULTS"))
        status = data.get("status", "")
        answered = status in ("OK", "ZERO_RESULTS")
        if status == "OK" and data.get("results"):
            r = data["results"][0]
            loc = r["geometry"]["location"]
            return Place(loc["lat"], loc["lng"], r.get("formatted_address", location),
                         _google_doubt(r, location)), answered
        if not answered:
            errors.append(f"Google geocoding: {status} {data.get('error_message', '')}".strip())
        return None, answered
    except (HttpError, KeyError) as exc:
        errors.append(str(exc))
        return None, False


def _free_lookup(location: str, elsewhere: bool, errors: list[str]) -> Lookup:
    """The free lookup (_nominatim): in Utah first, unless another state is named, then
    anywhere in the US."""
    answered = False
    for in_utah in ((False,) if elsewhere else (True, False)):
        try:
            found = _nominatim(location, in_utah)
        except (HttpError, KeyError, ValueError) as exc:
            errors.append(str(exc))
            continue
        answered = True
        if found:
            return found, True
    return None, answered


def _not_found(location: str) -> GeocodeError:
    return GeocodeError(f"Could not find the place '{shown(location)}'. Check the spelling, or try "
                        "a ZIP code, city or street address.")


def geocode(location: str, api_key: str | None = None) -> Found:
    """Return (lat, lon, label) for a ZIP code, city, address, or "lat,lon".

    A place that doesn't name another state is looked for in Utah first, so a bare
    "Murray" is Murray, Utah (the Salt Lake area's towns are known offline, see
    UTAH_PLACES); the label names the place found, for the page to show before the
    search starts. A place found online is a Place: its `doubt` says when the match is
    only a road, a trail, a park or a business, not a town, ZIP code or street address.
    Text with no letter that isn't a ZIP code (a date, a bare number) is not looked up,
    and neither is a ZIP code the ZIP lookup says doesn't exist: both are not found."""
    location = (location or "").strip() or config.DEFAULT_LOCATION
    found = _coordinates(location) or _KNOWN.get(_place_key(location))
    if found:
        return Place(*found)           # typed as coordinates, or known offline: no doubt
    errors: list[str] = []
    answered = False          # a lookup answered (even "no such place"), so it isn't an outage
    zm = _ZIP.match(location)
    if zm:
        found, answered = _zip_lookup(zm.group(1), errors)
        if found:
            return found
        if answered:
            raise _not_found(location)           # five digits, but no such ZIP code
    elif not _LETTER.search(location):
        raise _not_found(location)               # "1900-01-01", "12345678": not a place
    elsewhere = names_another_state(location)
    if api_key is None:        # "" means the caller resolved that there is no Google key
        api_key = os.environ.get("GOOGLE_PLACES_API_KEY", "")
    if api_key:
        found, said = _google_lookup(location, api_key, elsewhere, errors)
        answered = answered or said
        if found:
            return found
    found, said = _free_lookup(location, elsewhere, errors)
    if found:
        return found
    if errors:
        log.info("Geocoding %r failed: %s", location, "; ".join(errors))
    if not (answered or said):
        # Every lookup failed (the services are down or unreachable): not a typo.
        raise LookupDown(f"Couldn't look up '{shown(location)}' right now: the place-lookup "
                         "services aren't answering. Nothing was spent, and today's search is "
                         "still available. Try again in a minute.")
    raise _not_found(location)


def _nominatim(location: str, in_utah: bool) -> Place | None:
    """The free OpenStreetMap lookup: the best match in the US, or (in_utah) only in
    Utah, preferring a town, ZIP code or street address over a street or business of the
    same name, then any town-like match (class place or boundary). With its address
    parts (addressdetails), so a doubtful match can name the town it is in."""
    params: dict[str, str | int] = {"q": location, "format": "json", "countrycodes": "us",
                                    "addressdetails": 1, "limit": 5 if in_utah else 1}
    if in_utah:
        params.update(viewbox=",".join(str(v) for v in UTAH_BOX), bounded=1)
    data = request_json("GET", "https://nominatim.openstreetmap.org/search", params=params,
                        retries=2, cacheable=lambda v: isinstance(v, list))
    if not data:
        return None
    best = (next((d for d in data if not _osm_doubt(d, location)), None)
            or next((d for d in data if d.get("class") in ("place", "boundary")), data[0]))
    return Place(float(best["lat"]), float(best["lon"]), best.get("display_name", location),
                 _osm_doubt(best, location))
