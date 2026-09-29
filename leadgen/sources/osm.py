"""Free fallback source: OpenStreetMap via the Overpass API (no key needed).

Coverage is thinner than Google (fewer phone numbers and websites) but it
often includes warehouses and industrial buildings that Google lists poorly,
and building outlines give a rough size signal.
"""

import logging
import math
import re
import time

from .. import config
from ..geo import METERS_PER_MILE
from ..http import HttpError, request_json
from ..models import Lead
from . import SourceError

log = logging.getLogger(__name__)

# Tag keys copied onto each lead so the scorer can classify it.
CATEGORY_KEYS = ["shop", "amenity", "building", "industrial", "craft", "man_made", "office",
                 "tourism", "leisure", "aeroway", "healthcare", "landuse", "military"]
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
SQFT_PER_M2 = 10.7639
_NOT_PLACES = ["highway", "waterway", "railway", "route", "boundary", "natural",
               "public_transport", "place", "power"]
MAX_BUILDING_SQFT = 3_000_000   # bigger bounding boxes are campuses/resorts, not buildings


def _tag_filters():
    """Group category tags into Overpass filters: {key: [values]} and bare keys."""
    by_key, any_value = {}, set()
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


def _ql_string(text):
    return (text.replace("\\", "\\\\").replace('"', '\\"')
            .replace("\n", "\\n").replace("\t", "\\t"))


def build_query(lat, lon, radius_miles, keywords=(), timeout=None):
    r = int(radius_miles * METERS_PER_MILE)
    around = f"(around:{r},{lat:.6f},{lon:.6f})"
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
    timeout = int(timeout or config.OVERPASS_DEADLINE_SECONDS)
    return (f"[out:json][timeout:{timeout}];\n(\n  {body}\n)->.all;\n"
            "node.all->.n;\n.n out body;\n"
            "(way.all; relation.all;)->.w;\n.w out tags bb;")


def _footprint_sqft(bounds):
    if not bounds:
        return None
    dlat = (bounds["maxlat"] - bounds["minlat"]) * 111320
    mid = math.radians((bounds["maxlat"] + bounds["minlat"]) / 2)
    dlon = (bounds["maxlon"] - bounds["minlon"]) * 111320 * math.cos(mid)
    area = dlat * dlon * SQFT_PER_M2
    return int(round(area, -2)) if area > 0 else None


def _pretty(tag_value):
    return tag_value.replace("_", " ").replace(";", ", ").title()


_LIFECYCLE = ("disused:", "abandoned:", "was:", "demolished:")
_USE_KEYS = [k for k in CATEGORY_KEYS if k != "building"]


def _only_former_use(tags):
    """True when a lifecycle prefix (disused:shop=...) records the only use left."""
    former = any(k.startswith(_LIFECYCLE) and k.split(":", 1)[1] in _USE_KEYS for k in tags)
    return former and not any(k in tags for k in _USE_KEYS)


def _fetched_by_tag(tags):
    by_key, any_value = _tag_filters()
    return (any(v in values for k, values in by_key.items() for v in tags.get(k, "").split(";"))
            or any(k in tags for k in any_value))


def parse_element(el):
    tags = el.get("tags", {})
    name = tags.get("name", "").strip()
    # Skip unnamed features and per-building labels like "B" or "12" inside a
    # complex (but keep brands like "3M").
    if len(name) < 2 or not any(ch.isalpha() for ch in name):
        return None
    # Places that no longer operate.
    if "(historical)" in name.lower() or _only_former_use(tags) or any(
            tags.get(k) == "yes" for k in ("disused", "abandoned", "demolished")):
        return None
    # Roads, rivers, place labels etc. only slip in through the name query; an
    # element a category-tag query fetched (a rail depot, an apartment node) stays.
    if any(k in tags for k in _NOT_PLACES) and not _fetched_by_tag(tags):
        return None
    if "lat" in el and "lon" in el:
        lat, lon = el["lat"], el["lon"]
    elif "bounds" in el:
        b = el["bounds"]
        lat, lon = (b["minlat"] + b["maxlat"]) / 2, (b["minlon"] + b["maxlon"]) / 2
    else:
        return None
    raw = [f"{k}={tags[k]}" for k in CATEGORY_KEYS if k in tags]
    for k in ("brand", "operator"):
        if k in tags:
            raw.append(f"{k}={tags[k]}")
    primary = ""
    for k in CATEGORY_KEYS:
        if k in tags and tags[k] not in ("yes",):
            primary = _pretty(tags[k])
            break
    street = " ".join(x for x in [tags.get("addr:housenumber", ""), tags.get("addr:street", "")] if x)
    etype = el.get("type", "node")
    footprint = None
    if etype in ("way", "relation") and ("building" in tags or "shop" in tags):
        footprint = _footprint_sqft(el.get("bounds"))
        if footprint and footprint > MAX_BUILDING_SQFT:
            footprint = None
    return Lead(
        name=name,
        lat=lat,
        lon=lon,
        source="osm",
        source_id=f"{etype}/{el.get('id')}",
        address=street,
        city=tags.get("addr:city", ""),
        state=tags.get("addr:state", ""),
        zip=tags.get("addr:postcode", ""),
        phone=tags.get("phone", tags.get("contact:phone", "")),
        website=tags.get("website", tags.get("contact:website", "")),
        raw_categories=raw,
        primary_category=primary,
        footprint_sqft=footprint,
        business_status="CLOSED_PERMANENTLY" if tags.get("disused") == "yes" else "",
        map_url=f"https://www.openstreetmap.org/{etype}/{el.get('id')}",
    )


def search(lat, lon, radius_miles, keywords=(), progress=None):
    """Return (leads, warnings).

    Every mirror together gets config.OVERPASS_DEADLINE_SECONDS: when the map
    servers are down the search fails within about two minutes, not ten.
    """
    query = build_query(lat, lon, radius_miles, keywords)
    deadline = time.monotonic() + config.OVERPASS_DEADLINE_SECONDS
    errors = []
    servers = len(config.OVERPASS_ENDPOINTS)
    for n, endpoint in enumerate(config.OVERPASS_ENDPOINTS, start=1):
        left = deadline - time.monotonic()
        if left < 5:
            errors.append("out of time before trying " + endpoint)
            break
        if progress:
            progress(f"OpenStreetMap: searching the free map data (server {n} of {servers})")
        log.info("OpenStreetMap: querying %s", endpoint.split("/")[2])
        try:
            # Connecting gets at most 10 s; the answer may take the rest of the time.
            data = request_json("POST", endpoint, data={"data": query},
                                timeout=(min(10.0, left), left), retries=1,
                                cache_key_extra="overpass",
                                cacheable=lambda d: not d.get("remark"))
        except HttpError as exc:
            log.warning("OpenStreetMap server failed: %s", exc)
            errors.append(str(exc))
            continue
        if data.get("remark") and not data.get("elements"):
            log.warning("OpenStreetMap server %s: %s", endpoint, data["remark"])
            errors.append(f"{endpoint}: {data['remark']}")
            continue
        leads = [lead for lead in map(parse_element, data.get("elements", [])) if lead]
        warnings = []
        if data.get("remark"):
            log.warning("OpenStreetMap note: %s", data["remark"])
            warnings.append("The map data service returned only part of its results "
                            "(it was busy).")
        return leads, warnings
    raise SourceError("All OpenStreetMap (Overpass) servers failed: " + " | ".join(errors))
