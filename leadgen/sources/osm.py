"""Free fallback source: OpenStreetMap via the Overpass API (no key needed).

Coverage is thinner than Google (fewer phone numbers and websites) but it
often includes warehouses and industrial buildings that Google lists poorly,
and building outlines give a rough size signal.
"""

import math
import re

from .. import config
from ..geo import METERS_PER_MILE
from ..http import HttpError, request_json
from ..models import Lead
from . import SourceError

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


def _tag_filters():
    """Group category tags into Overpass filters: {key: [values]} and bare keys."""
    by_key, any_value = {}, set()
    for cat in config.CATEGORIES:
        for key, value in cat.osm_tags:
            if (key, value) in _SKIP_QUERY:
                continue
            if value is None:
                any_value.add(key)
            else:
                by_key.setdefault(key, set()).add(value)
    return by_key, any_value


def build_query(lat, lon, radius_miles, keywords=()):
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
    regex = "|".join(sorted({re.escape(t).replace('"', "") for t in terms}))
    parts.append(f'nwr["name"~"{regex}",i]{around};')
    body = "\n  ".join(parts)
    return (f"[out:json][timeout:180];\n(\n  {body}\n)->.all;\n"
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


def parse_element(el):
    tags = el.get("tags", {})
    name = tags.get("name", "").strip()
    if not name:
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
    if etype in ("way", "relation") and ("building" in tags or "shop" in tags
                                         or "amenity" in tags or "industrial" in tags):
        footprint = _footprint_sqft(el.get("bounds"))
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
    """Return (leads, warnings)."""
    query = build_query(lat, lon, radius_miles, keywords)
    errors = []
    for endpoint in config.OVERPASS_ENDPOINTS:
        if progress:
            progress(f"OpenStreetMap: querying {endpoint.split('/')[2]}")
        try:
            data = request_json("POST", endpoint, data={"data": query}, timeout=200, retries=2,
                                cache_key_extra="overpass",
                                cacheable=lambda d: not d.get("remark"))
        except HttpError as exc:
            errors.append(str(exc))
            continue
        if data.get("remark") and not data.get("elements"):
            errors.append(f"{endpoint}: {data['remark']}")
            continue
        leads = [lead for lead in map(parse_element, data.get("elements", [])) if lead]
        warnings = [f"OpenStreetMap note: {data['remark']}"] if data.get("remark") else []
        return leads, warnings
    raise SourceError("All OpenStreetMap (Overpass) servers failed: " + " | ".join(errors))
