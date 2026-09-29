"""Google Places API (New) text search.

Each search phrase is run in every grid cell, following up to 3 result pages
(Google's 60-result cap). Results outside the requested radius are dropped
later by the pipeline.
"""

import json
import re
from collections.abc import Callable, Sequence
from typing import Any

from .. import config
from ..geo import METERS_PER_MILE, search_grid
from ..http import request_json
from ..models import Lead
from . import SourceError
from .paging import Cell, run_searches

SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = ",".join([
    "places.id", "places.displayName", "places.formattedAddress", "places.addressComponents",
    "places.location", "places.types", "places.primaryType", "places.primaryTypeDisplayName",
    "places.nationalPhoneNumber", "places.websiteUri", "places.userRatingCount",
    "places.businessStatus", "places.googleMapsUri", "nextPageToken",
])
MAX_BIAS_RADIUS_M = 50000.0
MAX_PAGES = 3
TOKEN_DELAY_SECONDS = 2.0   # give Google a moment before a page token is usable


_CITY_STATE_ZIP = re.compile(r",\s*([^,]+),\s*([A-Z]{2})\s+(\d{5})(?:-\d{4})?(?:,\s*USA)?\s*$")


def _split_formatted(address: str) -> tuple[str, str, str]:
    """'1 Main St, Salt Lake City, UT 84101, USA' -> ('Salt Lake City', 'UT', '84101')."""
    m = _CITY_STATE_ZIP.search(address or "")
    return (m.group(1).strip(), m.group(2), m.group(3)) if m else ("", "", "")


def parse_place(place: dict[str, Any], query: str = "") -> Lead:
    comps: dict[str, dict[str, Any]] = {}
    for c in place.get("addressComponents", []):
        for t in c.get("types", []):
            comps.setdefault(t, c)
    street = " ".join(x for x in [comps.get("street_number", {}).get("longText", ""),
                                  comps.get("route", {}).get("shortText", "")] if x)
    loc = place.get("location", {})
    city, state, zip_code = _split_formatted(place.get("formattedAddress", ""))
    return Lead(
        name=(place.get("displayName") or {}).get("text", ""),
        lat=loc.get("latitude"),
        lon=loc.get("longitude"),
        source="google",
        source_id=place.get("id", ""),
        address=street or place.get("formattedAddress", "").split(",")[0],
        city=comps.get("locality", comps.get("sublocality", {})).get("longText", "") or city,
        state=comps.get("administrative_area_level_1", {}).get("shortText", "") or state,
        zip=comps.get("postal_code", {}).get("longText", "") or zip_code,
        phone=place.get("nationalPhoneNumber", ""),
        website=place.get("websiteUri", ""),
        raw_categories=list(place.get("types", [])),
        primary_category=(place.get("primaryTypeDisplayName") or {}).get("text", ""),
        rating_count=place.get("userRatingCount"),
        business_status=place.get("businessStatus", ""),
        map_url=place.get("googleMapsUri", ""),
        search_terms=[query] if query else [],
    )


def estimate_requests(queries: Sequence[str], grid_cells: int) -> int:
    """Upper bound on requests for a run (every search following every page)."""
    return len(queries) * len(search_grid(0, 0, 1, grid_cells)) * MAX_PAGES


def search(lat: float, lon: float, radius_miles: float, queries: Sequence[str], api_key: str,
           grid_cells: int = 1, max_requests: int | None = None,
           progress: Callable[[str], None] | None = None) -> tuple[list[Lead], int, list[str]]:
    """Return (leads, requests_made, warnings). Put the most important queries first."""
    if not api_key:
        raise SourceError("No Google Places API key configured")
    headers = {"X-Goog-Api-Key": api_key, "X-Goog-FieldMask": FIELD_MASK,
               "Content-Type": "application/json"}
    cells = search_grid(lat, lon, radius_miles, grid_cells)
    if max_requests is None:
        max_requests = len(queries) * len(cells) * MAX_PAGES

    def fetch_page(query: str, cell: Cell, token: str | None) -> tuple[list[Any], str | None]:
        clat, clon, crad = cell
        body = {
            "textQuery": query,
            "pageSize": 20,
            "locationBias": {"circle": {
                "center": {"latitude": clat, "longitude": clon},
                "radius": min(crad * METERS_PER_MILE, MAX_BIAS_RADIUS_M),
            }},
        }
        if token:
            body["pageToken"] = token
        data = request_json("POST", SEARCH_URL, json_body=body, headers=headers, use_cache=False)
        return data.get("places", []), data.get("nextPageToken")

    def chain_key(query: str, cell: Cell) -> str:
        clat, clon, crad = cell
        return json.dumps(["google-chain-v1", FIELD_MASK, query, round(clat, 5), round(clon, 5),
                           round(crad, 3)])

    return run_searches("Google", queries, cells, fetch_page, parse_place,
                        max_pages=MAX_PAGES, max_requests=max_requests,
                        cache_version=["google-chain-v1", FIELD_MASK], cache_key=chain_key,
                        progress=progress, stop_check=lambda: config.stop_reason("google"),
                        token_delay=TOKEN_DELAY_SECONDS,
                        key_errors=("API_KEY_INVALID", "API key not valid"))
