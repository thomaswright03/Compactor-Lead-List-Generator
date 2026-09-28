"""Google Places API (New) text search.

Each search phrase is run in every grid cell, following up to 3 result pages
(Google's 60-result cap). Results outside the requested radius are dropped
later by the pipeline.
"""

import json
import re
import time

from ..geo import METERS_PER_MILE, search_grid
from ..http import HttpError, cache_get, cache_put, request_json
from ..models import Lead
from . import SourceError

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


def _split_formatted(address):
    """'1 Main St, Salt Lake City, UT 84101, USA' -> ('Salt Lake City', 'UT', '84101')."""
    m = _CITY_STATE_ZIP.search(address or "")
    return (m.group(1).strip(), m.group(2), m.group(3)) if m else ("", "", "")


def parse_place(place, query=""):
    comps = {}
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


def _chain_key(query, clat, clon, crad):
    return json.dumps(["google-chain-v1", FIELD_MASK, query, round(clat, 5), round(clon, 5),
                       round(crad, 3)])


def estimate_requests(queries, grid_cells):
    """Upper bound on requests for a run (every search following every page)."""
    return len(queries) * len(search_grid(0, 0, 1, grid_cells)) * MAX_PAGES


def search(lat, lon, radius_miles, queries, api_key, grid_cells=1, max_requests=None,
           progress=None):
    """Return (leads, requests_made, warnings).

    Queries are searched in the order given, so put the most important first.
    The request budget is spent breadth-first: page 1 of every (query, cell)
    search before any page 2, so a low cap trims depth, not whole categories.
    Complete result chains are cached as a unit, so a re-run never replays an
    old page token.
    """
    if not api_key:
        raise SourceError("No Google Places API key configured")
    headers = {"X-Goog-Api-Key": api_key, "X-Goog-FieldMask": FIELD_MASK,
               "Content-Type": "application/json"}
    cells = search_grid(lat, lon, radius_miles, grid_cells)
    if max_requests is None:
        max_requests = len(queries) * len(cells) * MAX_PAGES
    leads, warnings, requests_made = [], [], 0

    chains = []
    for q in queries:
        for clat, clon, crad in cells:
            key = _chain_key(q, clat, clon, crad)
            cached = cache_get(key)
            if cached is not None:
                leads += [lead for lead in (parse_place(p, q) for p in cached)
                          if lead.lat is not None and lead.lon is not None]
                continue
            chains.append({"q": q, "cell": (clat, clon, crad), "key": key, "places": [],
                           "token": None, "token_at": 0.0, "pages": 0, "failed": False})

    if len(chains) > max_requests:
        warnings.append(
            f"Google request cap ({max_requests}) is below the {len(chains)} searches needed "
            f"({len(queries)} phrases x {len(cells)} areas); the last {len(chains) - max_requests} "
            f"searches were skipped. Raise --max-requests to about {len(chains) * MAX_PAGES} "
            "for full coverage.")

    capped = False
    for page in range(MAX_PAGES):
        active = [c for c in chains if not c["failed"] and (page == 0 or c["token"])]
        for n, chain in enumerate(active, start=1):
            if requests_made >= max_requests:
                capped = True
                break
            clat, clon, crad = chain["cell"]
            if progress:
                progress(f"Google page {page + 1}: '{chain['q']}' ({n}/{len(active)})")
            body = {
                "textQuery": chain["q"],
                "pageSize": 20,
                "locationBias": {"circle": {
                    "center": {"latitude": clat, "longitude": clon},
                    "radius": min(crad * METERS_PER_MILE, MAX_BIAS_RADIUS_M),
                }},
            }
            if chain["token"]:
                body["pageToken"] = chain["token"]
                wait = TOKEN_DELAY_SECONDS - (time.time() - chain["token_at"])
                if wait > 0:
                    time.sleep(wait)
            requests_made += 1
            try:
                data = request_json("POST", SEARCH_URL, json_body=body, headers=headers,
                                    use_cache=False)
            except HttpError as exc:
                msg = str(exc)
                auth_error = "HTTP 401" in msg or "HTTP 403" in msg
                first_request_rejected = "HTTP 400" in msg and requests_made == 1
                if auth_error or first_request_rejected:
                    raise SourceError("Google Places rejected the request. Check the API key and "
                                      f"that 'Places API (New)' is enabled. {msg}")
                what = "a later results page" if chain["token"] else "the search"
                warnings.append(f"Google '{chain['q']}': {what} failed ({msg[:160]}); "
                                "kept the results already found")
                chain["failed"] = True
                chain["token"] = None
                continue
            places = data.get("places", [])
            chain["places"] += places
            chain["pages"] += 1
            chain["token"] = data.get("nextPageToken")
            chain["token_at"] = time.time()
            leads += [lead for lead in (parse_place(p, chain["q"]) for p in places)
                      if lead.lat is not None and lead.lon is not None]
        if capped:
            break

    if capped:
        skipped = sum(1 for c in chains if c["pages"] == 0 and not c["failed"])
        more = sum(1 for c in chains if c["token"])
        warnings.append(f"Stopped at the {max_requests}-request Google cap: {skipped} searches "
                        f"not run, {more} had more result pages. Raise --max-requests for "
                        "more coverage.")

    for chain in chains:
        complete = not chain["token"] or chain["pages"] >= MAX_PAGES
        if chain["pages"] and complete and not chain["failed"]:
            cache_put(chain["key"], chain["places"])
    return leads, requests_made, warnings
