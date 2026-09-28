"""Google Places API (New) text search.

Each search phrase is run in every grid cell, following up to 3 result pages
(Google's 60-result cap). Results outside the requested radius are dropped
later by the pipeline.
"""

from ..geo import METERS_PER_MILE, search_grid
from ..http import HttpError, request_json
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


def parse_place(place, query=""):
    comps = {}
    for c in place.get("addressComponents", []):
        for t in c.get("types", []):
            comps.setdefault(t, c)
    street = " ".join(x for x in [comps.get("street_number", {}).get("longText", ""),
                                  comps.get("route", {}).get("shortText", "")] if x)
    loc = place.get("location", {})
    return Lead(
        name=(place.get("displayName") or {}).get("text", ""),
        lat=loc.get("latitude"),
        lon=loc.get("longitude"),
        source="google",
        source_id=place.get("id", ""),
        address=street or place.get("formattedAddress", "").split(",")[0],
        city=comps.get("locality", comps.get("sublocality", {})).get("longText", ""),
        state=comps.get("administrative_area_level_1", {}).get("shortText", ""),
        zip=comps.get("postal_code", {}).get("longText", ""),
        phone=place.get("nationalPhoneNumber", ""),
        website=place.get("websiteUri", ""),
        raw_categories=list(place.get("types", [])),
        primary_category=(place.get("primaryTypeDisplayName") or {}).get("text", ""),
        rating_count=place.get("userRatingCount"),
        business_status=place.get("businessStatus", ""),
        map_url=place.get("googleMapsUri", ""),
        search_terms=[query] if query else [],
    )


def search(lat, lon, radius_miles, queries, api_key, grid_cells=1, max_requests=150,
           progress=None):
    """Return (leads, requests_made, warnings)."""
    if not api_key:
        raise SourceError("No Google Places API key configured")
    headers = {"X-Goog-Api-Key": api_key, "X-Goog-FieldMask": FIELD_MASK,
               "Content-Type": "application/json"}
    leads, warnings, requests_made = [], [], 0
    cells = search_grid(lat, lon, radius_miles, grid_cells)
    total = len(cells) * len(queries)
    done = 0
    for q in queries:
        for clat, clon, crad in cells:
            done += 1
            if progress:
                progress(f"Google: '{q}' ({done}/{total})")
            token = None
            for _ in range(MAX_PAGES):
                if requests_made >= max_requests:
                    warnings.append(f"Stopped Google search at the {max_requests}-request cap; "
                                    "raise --max-requests for more coverage")
                    return leads, requests_made, warnings
                body = {
                    "textQuery": q,
                    "pageSize": 20,
                    "locationBias": {"circle": {
                        "center": {"latitude": clat, "longitude": clon},
                        "radius": min(crad * METERS_PER_MILE, MAX_BIAS_RADIUS_M),
                    }},
                }
                if token:
                    body["pageToken"] = token
                try:
                    requests_made += 1
                    data = request_json("POST", SEARCH_URL, json_body=body, headers=headers,
                                        cache_key_extra="google-places-v1")
                except HttpError as exc:
                    msg = str(exc)
                    if "HTTP 400" in msg or "HTTP 401" in msg or "HTTP 403" in msg:
                        raise SourceError(f"Google Places rejected the request. Check the API key "
                                          f"and that 'Places API (New)' is enabled. {msg}")
                    warnings.append(f"Google search '{q}' failed: {msg}")
                    break
                for place in data.get("places", []):
                    lead = parse_place(place, q)
                    if lead.lat is not None and lead.lon is not None:
                        leads.append(lead)
                token = data.get("nextPageToken")
                if not token:
                    break
    return leads, requests_made, warnings
