"""A map element (an Overpass node, way or relation) as a business: its name, position,
address, category tags and building size, or nothing when it is no business to call."""

import math
import re
from typing import Any

from ..models import Lead
from ..scoring import generic_name
from .osm_query import _NOT_PLACES, _tag_filters

# Tag keys copied onto each lead so the scorer can classify it.
CATEGORY_KEYS = ["shop", "amenity", "building", "industrial", "craft", "man_made", "office",
                 "tourism", "leisure", "aeroway", "healthcare", "landuse", "military"]
SQFT_PER_M2 = 10.7639
MAX_BUILDING_SQFT = 3_000_000   # bigger bounding boxes are campuses/resorts, not buildings


def _footprint_sqft(bounds: dict[str, float] | None) -> int | None:
    if not bounds:
        return None
    dlat = (bounds["maxlat"] - bounds["minlat"]) * 111320
    mid = math.radians((bounds["maxlat"] + bounds["minlat"]) / 2)
    dlon = (bounds["maxlon"] - bounds["minlon"]) * 111320 * math.cos(mid)
    area = dlat * dlon * SQFT_PER_M2
    return int(round(area, -2)) if area > 0 else None


def _outline(bounds: dict[str, float] | None) -> list[float] | None:
    """A map area's bounding box as [south, west, north, east] (dedupe.py joins the
    listings of one site that lie inside it), or None without usable bounds."""
    try:
        box = [round(float(bounds[k]), 6) for k in ("minlat", "minlon", "maxlat", "maxlon")]  # type: ignore[index]
    except (KeyError, TypeError, ValueError):
        return None
    return box if box[0] <= box[2] and box[1] <= box[3] else None


def _pretty(tag_value: str) -> str:
    return tag_value.replace("_", " ").replace(";", ", ").title()


_LIFECYCLE = ("disused:", "abandoned:", "was:", "demolished:")
_USE_KEYS = [k for k in CATEGORY_KEYS if k != "building"]


def _only_former_use(tags: dict[str, str]) -> bool:
    """True when a lifecycle prefix (disused:shop=...) records the only use left."""
    former = any(k.startswith(_LIFECYCLE) and k.split(":", 1)[1] in _USE_KEYS for k in tags)
    return former and not any(k in tags for k in _USE_KEYS)


def _fetched_by_tag(tags: dict[str, str]) -> bool:
    by_key, any_value = _tag_filters()
    return (any(v in values for k, values in by_key.items() for v in tags.get(k, "").split(";"))
            or any(k in tags for k in any_value))


# Labels for one part of a complex ("Building B", "Bldg 3", "Tower 2", "C Building"):
# not a business, and the complex itself is its own element when it is mapped.
_PART_LABEL = re.compile(
    r"^(?:(?:building|bldg\.?|block|wing|tower|unit|suite|ste\.?|phase|annex|hall)\s*[#-]?\s*"
    r"[0-9a-z]{1,4}|[0-9a-z]{1,3}\s*[-]?\s*(?:building|bldg\.?|wing|tower))$", re.I)


def is_part_label(name: str) -> bool:
    """True for a name that only labels part of a complex, like "Building B"."""
    return bool(_PART_LABEL.match(name.strip()))


def _not_a_business(name: str, tags: dict[str, str]) -> bool:
    """True for a map element that is no business to call: unnamed, a label of part of
    a complex, no longer operating, or a road, river or place label."""
    # Skip unnamed features and per-building labels like "B", "12" or "Building B"
    # inside a complex (but keep brands like "3M").
    if len(name) < 2 or not any(ch.isalpha() for ch in name) or is_part_label(name):
        return True
    # Places that no longer operate.
    if "(historical)" in name.lower() or _only_former_use(tags) or any(
            tags.get(k) == "yes" for k in ("disused", "abandoned", "demolished")):
        return True
    # Roads, rivers, place labels etc. only slip in through the name query; an
    # element a category-tag query fetched (a rail depot, an apartment node) stays.
    return any(k in tags for k in _NOT_PLACES) and not _fetched_by_tag(tags)


def _position(el: dict[str, Any]) -> tuple[float, float] | None:
    """The element's (lat, lon): a node's own, or the middle of a way's bounds."""
    if "lat" in el and "lon" in el:
        return el["lat"], el["lon"]
    if "bounds" in el:
        b = el["bounds"]
        return (b["minlat"] + b["maxlat"]) / 2, (b["minlon"] + b["maxlon"]) / 2
    return None


def _categories(tags: dict[str, str]) -> tuple[list[str], str]:
    """The element's category tags (and brand or operator) as "key=value" strings for
    the scorer, and its first real category, prettied, as the primary one."""
    raw = [f"{k}={tags[k]}" for k in CATEGORY_KEYS if k in tags]
    for k in ("brand", "operator"):
        if k in tags:
            raw.append(f"{k}={tags[k]}")
    primary = ""
    for k in CATEGORY_KEYS:
        if k in tags and tags[k] not in ("yes",):
            primary = _pretty(tags[k])
            break
    return raw, primary


def _footprint(el: dict[str, Any], tags: dict[str, str], etype: str) -> int | None:
    """A building's rough size from its bounds (sq ft), when it is a building."""
    if etype not in ("way", "relation") or not ("building" in tags or "shop" in tags):
        return None
    footprint = _footprint_sqft(el.get("bounds"))
    return None if footprint and footprint > MAX_BUILDING_SQFT else footprint


def parse_element(el: dict[str, Any]) -> Lead | None:
    """A business from one Overpass element (a node, way or relation), or None when it
    is no business or has no position."""
    tags = el.get("tags", {})
    name = tags.get("name", "").strip()
    if _not_a_business(name, tags):
        return None
    at = _position(el)
    if at is None:
        return None
    lat, lon = at
    raw, primary = _categories(tags)
    street = " ".join(x for x in [tags.get("addr:housenumber", ""), tags.get("addr:street", "")] if x)
    if generic_name(name):
        name = _descriptive(name, tags, street)
    etype = el.get("type", "node")
    outline = _outline(el.get("bounds")) if etype in ("way", "relation") else None
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
        footprint_sqft=_footprint(el, tags, etype),
        outline=outline,
        business_status="CLOSED_PERMANENTLY" if tags.get("disused") == "yes" else "",
        map_url=f"https://www.openstreetmap.org/{etype}/{el.get('id')}",
    )


def _descriptive(name: str, tags: dict[str, str], street: str) -> str:
    """A generic map name ("Recycling") made into something a salesperson can look up:
    with its operator or brand ("Recycling (Salt Lake County)"), else its street
    ("Recycling at 1200 W 500 S"), else its city ("Recycling, Magna")."""
    who = (tags.get("operator") or tags.get("brand") or "").strip()
    if who and not generic_name(who):
        return f"{name} ({who})"
    if street:
        return f"{name} at {street}"
    city = tags.get("addr:city", "").strip()
    return f"{name}, {city}" if city else name
