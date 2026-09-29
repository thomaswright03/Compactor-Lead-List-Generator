"""Turn a ZIP / city / "lat,lon" into coordinates and measure distances."""

import logging
import math
import os
import re

from . import config
from .http import HttpError, request_json

log = logging.getLogger(__name__)

EARTH_RADIUS_MILES = 3958.8
METERS_PER_MILE = 1609.344

# Works offline for Arco's shop, the default area and common nearby cities.
KNOWN_PLACES = {
    config.OWN_ADDRESS.lower(): config.OWN_COORDS,
    "876 fortune rd": config.OWN_COORDS,
    "876 fortune road, salt lake city, ut 84104": config.OWN_COORDS,
    "1876 w fortune rd, salt lake city, ut 84104": config.OWN_COORDS,
    "arco": config.OWN_COORDS,
    "arco compactor": config.OWN_COORDS,
    "salt lake city": (40.7608, -111.8910),
    "salt lake city, ut": (40.7608, -111.8910),
    "slc": (40.7608, -111.8910),
    "west valley city, ut": (40.6916, -112.0011),
    "west jordan, ut": (40.6097, -111.9391),
    "sandy, ut": (40.5649, -111.8389),
    "south jordan, ut": (40.5622, -111.9297),
    "ogden, ut": (41.2230, -111.9738),
    "provo, ut": (40.2338, -111.6585),
    "orem, ut": (40.2969, -111.6946),
    "layton, ut": (41.0602, -111.9711),
    "draper, ut": (40.5247, -111.8638),
    "murray, ut": (40.6669, -111.8880),
    "lehi, ut": (40.3916, -111.8508),
    "bountiful, ut": (40.8894, -111.8808),
    "tooele, ut": (40.5308, -112.2983),
}


class GeocodeError(ValueError):
    pass


def _place_key(text: str) -> str:
    """'876 Fortune Rd,  Salt Lake City, UT' -> '876 fortune rd salt lake city ut'."""
    return " ".join(re.sub(r"[,.]", " ", text.lower()).split())


_KNOWN = {_place_key(k): v for k, v in KNOWN_PLACES.items()}


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


def geocode(location: str, api_key: str | None = None) -> tuple[float, float, str]:
    """Return (lat, lon, label) for a ZIP code, city, address, or "lat,lon"."""
    location = (location or "").strip() or config.DEFAULT_LOCATION
    m = _LATLON.match(location)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise GeocodeError(f"Coordinates out of range: {location}")
        return lat, lon, location

    known = _KNOWN.get(_place_key(location))
    if known:
        return known[0], known[1], location

    errors = []
    zm = _ZIP.match(location)
    if zm:
        try:
            data = request_json("GET", f"https://api.zippopotam.us/us/{zm.group(1)}", retries=2)
            place = data["places"][0]
            label = f"{place['place name']}, {place['state abbreviation']} {zm.group(1)}"
            return float(place["latitude"]), float(place["longitude"]), label
        except (HttpError, KeyError, IndexError, ValueError) as exc:
            errors.append(str(exc))

    if api_key is None:        # "" means the caller resolved that there is no Google key
        api_key = os.environ.get("GOOGLE_PLACES_API_KEY", "")
    if api_key:
        try:
            data = request_json("GET", "https://maps.googleapis.com/maps/api/geocode/json",
                                params={"address": location, "key": api_key}, retries=2,
                                cache_key_extra="google",
                                cacheable=lambda v: isinstance(v, dict)
                                and v.get("status") in ("OK", "ZERO_RESULTS"))
            status = data.get("status", "")
            if status == "OK" and data.get("results"):
                r = data["results"][0]
                loc = r["geometry"]["location"]
                return loc["lat"], loc["lng"], r.get("formatted_address", location)
            if status not in ("OK", "ZERO_RESULTS"):
                errors.append(f"Google geocoding: {status} {data.get('error_message', '')}".strip())
        except (HttpError, KeyError) as exc:
            errors.append(str(exc))

    try:
        data = request_json("GET", "https://nominatim.openstreetmap.org/search",
                            params={"q": location, "format": "json", "limit": 1,
                                    "countrycodes": "us"}, retries=2,
                            cacheable=lambda v: isinstance(v, list))
        if data:
            return float(data[0]["lat"]), float(data[0]["lon"]), data[0].get("display_name", location)
    except (HttpError, KeyError, ValueError) as exc:
        errors.append(str(exc))

    if errors:
        log.info("Geocoding %r failed: %s", location, "; ".join(errors))
    raise GeocodeError(f"Could not find the place '{location}'. Try a 5-digit ZIP code or a "
                       "city name.")
