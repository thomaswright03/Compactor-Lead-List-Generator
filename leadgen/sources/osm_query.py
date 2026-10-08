"""The Overpass query a map-data search asks: the categories' map tags (config.CATEGORIES)
and the name words that find a business, within the circle or one part of it."""

import re
from collections.abc import Sequence

from .. import config
from ..geo import METERS_PER_MILE
from .osm_areas import Box

# Area-type landuse tags produce huge, mostly unnamed polygons; only landfills are queried.
_SKIP_QUERY = {("landuse", "industrial"), ("landuse", "residential"), ("landuse", "military"),
               ("landuse", "logistics")}
_NAME_TERMS = [
    "compactor", "compaction", "baler", "baling", "recycl", "waste", "disposal", "sanitation",
    "distribution", "warehouse", "logistics", "fulfillment", "cold storage", "freight",
    "manufactur", "industries", "fabrication", "packaging", "printing", "corrugated",
    "foods", "meats", "dairy", "bottling", "brewing", "hospital", "medical center",
    "university", "college", "apartments",
]
# Map keys of roads, rivers, routes and place labels: a name match skips them (and
# osm_parse leaves out the ones it brings in anyway).
_NOT_PLACES = ["highway", "waterway", "railway", "route", "boundary", "natural",
               "public_transport", "place", "power"]


def _tag_filters() -> tuple[dict[str, set[str]], set[str]]:
    """Group category tags into Overpass filters: {key: [values]} and bare keys."""
    by_key: dict[str, set[str]] = {}
    any_value: set[str] = set()
    for cat in config.CATEGORIES:
        if not cat.query_osm:
            continue
        for key, value in cat.osm_tags:
            if (key, value) in _SKIP_QUERY:
                continue
            if value is None:
                any_value.add(key)
            else:
                by_key.setdefault(key, set()).add(value)
    return by_key, any_value


def _ql_string(text: str) -> str:
    return (text.replace("\\", "\\\\").replace('"', '\\"')
            .replace("\n", "\\n").replace("\t", "\\t"))


def build_query(lat: float, lon: float, radius_miles: float, keywords: Sequence[str] = (),
                timeout: int | None = None, box: Box | None = None) -> str:
    """The Overpass query for the circle, or (with box) for the part of it inside the box."""
    r = int(radius_miles * METERS_PER_MILE)
    around = f"(around:{r},{lat:.6f},{lon:.6f})"
    if box is not None:
        around += "({:.6f},{:.6f},{:.6f},{:.6f})".format(*box)
    by_key, any_value = _tag_filters()
    parts = []
    for key in sorted(by_key):
        values = "|".join(sorted(by_key[key]))
        parts.append(f'nwr["{key}"~"^({values})$"]["name"]{around};')
    for key in sorted(any_value):
        parts.append(f'nwr["{key}"]["name"]{around};')
    terms = list(_NAME_TERMS) + [k.lower() for k in keywords if k.strip()]
    # Escape twice: once for the regex, once for the Overpass QL string literal
    # (which would otherwise eat the regex backslashes).
    regex = "|".join(sorted({_ql_string(re.escape(t)) for t in terms}))
    # Name matches skip roads, canals, routes etc. ("University Pkwy", "Waste Ditch").
    not_places = "".join(f'[!"{k}"]' for k in _NOT_PLACES)
    parts.append(f'nwr["name"~"{regex}",i]{not_places}{around};')
    body = "\n  ".join(parts)
    timeout = int(timeout or config.OVERPASS_PART_SECONDS)
    return (f"[out:json][timeout:{timeout}];\n(\n  {body}\n)->.all;\n"
            "node.all->.n;\n.n out body;\n"
            "(way.all; relation.all;)->.w;\n.w out tags bb;")
