"""The areas a map-data search is asked in: the search circle cut into a grid of parts,
nearest the centre first, and each area named in words for the pages (osm.py asks for
them)."""

import math
from collections.abc import Sequence

from .. import config, places
from ..geo import haversine_miles

# An area to search, as Overpass writes it: (south, west, north, east) in degrees.
Box = tuple[float, float, float, float]
MILES_PER_DEGREE_LAT = 69.05


# One part of the area still to ask: (its box, or None for the whole circle; its
# share of the area; how many times it has been split into quarters).
Part = tuple[Box | None, float, int]


def _square(lat: float, lon: float, radius_miles: float) -> Box:
    """The search circle's bounding square."""
    dlat = radius_miles / MILES_PER_DEGREE_LAT
    dlon = dlat / max(0.01, math.cos(math.radians(lat)))
    return (lat - dlat, lon - dlon, lat + dlat, lon + dlon)


def _distance(lat: float, lon: float, box: Box | None) -> tuple[float, float]:
    """How far a part lies from the search centre, for asking the nearest first: miles
    to its nearest point (0 for the part the centre is in), then to its middle."""
    if box is None:
        return 0.0, 0.0
    near = haversine_miles(lat, lon, min(max(lat, box[0]), box[2]), min(max(lon, box[1]), box[3]))
    return near, haversine_miles(lat, lon, (box[0] + box[2]) / 2, (box[1] + box[3]) / 2)


def _parts(lat: float, lon: float, radius_miles: float) -> list[Box]:
    """The search circle's bounding square cut into a grid of boxes no wider than
    config.OVERPASS_PART_MILES, keeping only the boxes that reach into the circle,
    nearest the centre first (the part the centre is in, then outwards)."""
    n = max(1, math.ceil(2 * radius_miles / config.OVERPASS_PART_MILES))
    south, west, north, east = _square(lat, lon, radius_miles)
    step_lat, step_lon = (north - south) / n, (east - west) / n
    boxes = []
    for i in range(n):
        for j in range(n):
            box = (south + i * step_lat, west + j * step_lon,
                   south + (i + 1) * step_lat, west + (j + 1) * step_lon)
            # The box's point nearest the centre is inside the circle.
            if _distance(lat, lon, box)[0] <= radius_miles:
                boxes.append(box)
    return sorted(boxes, key=lambda box: _distance(lat, lon, box))


def _quarters(box: Box) -> list[Box]:
    s, w, n, e = box
    mid_lat, mid_lon = (s + n) / 2, (w + e) / 2
    return [(s, w, mid_lat, mid_lon), (s, mid_lon, mid_lat, e),
            (mid_lat, w, n, mid_lon), (mid_lat, mid_lon, n, e)]


def area_boxes(lat: float, lon: float, radius_miles: float) -> list[Box]:
    """The areas a search around (lat, lon) asks the map data in, nearest the centre
    first: the "N of 9 areas" of its progress and history (the Map page draws them)."""
    return _parts(lat, lon, radius_miles)


def area_name(lat: float, lon: float, radius_miles: float, box: Box) -> str:
    """An area of a search in words, as the pages name it ("Salt Lake City", "the
    area to the northwest"): area_map.py names every area with it."""
    return _area_name(lat, lon, radius_miles, box)


def part_boxes(parts: Sequence[Part]) -> list[list[float] | None]:
    """The parts' boxes as the day's record keeps them ([south, west, north, east],
    rounded; None for a search asked whole): which areas a search missed, for the Map
    page."""
    return [None if box is None else [round(x, 6) for x in box] for box, _, _ in parts]


def areas_left(parts: Sequence[Part], areas: int) -> int:
    """How many of a search's `areas` the missing parts make up (at least 1 while any
    part is missing), for the page's "still filling in N areas"."""
    share = sum(part[1] for part in parts)
    return max(1, round(share * areas)) if share > 0.001 else 0


_DIRECTIONS = ("north", "northeast", "east", "southeast", "south", "southwest", "west",
               "northwest")
# The words for the area a search's center is in when no town's center is in it.
CENTER_AREA = "the area around the search's center"


def _area_name(lat: float, lon: float, radius_miles: float, box: Box) -> str:
    """A part of a search's area in words: its biggest town ("Salt Lake City"), or
    where it lies from the center ("the area to the northwest") when it has none."""
    town = places.biggest_town(box, (lat, lon), radius_miles)
    if town:
        return town
    mid_lat, mid_lon = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    if box[0] <= lat <= box[2] and box[1] <= lon <= box[3]:
        return CENTER_AREA
    east = (mid_lon - lon) * math.cos(math.radians(lat))
    bearing = math.degrees(math.atan2(east, mid_lat - lat)) % 360
    return f"the area to the {_DIRECTIONS[round(bearing / 45) % 8]}"
