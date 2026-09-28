"""Yelp Fusion business search (needs YELP_API_KEY).

Yelp has phone numbers, addresses and review counts for most consumer-facing
businesses (grocery, retail, hotels, hospitals), but no company websites and
thin coverage of warehouses and plants, which the free map data fills in.
Yelp searches a radius of at most 40 km (~25 miles), so wider areas are
covered with the 7- or 19-cell grid.
"""

import re

from requests.structures import CaseInsensitiveDict

from .. import config
from ..geo import METERS_PER_MILE, search_grid
from ..http import HttpError, request_json
from ..models import Lead
from . import SourceError
from .paging import run_searches

SEARCH_URL = "https://api.yelp.com/v3/businesses/search"
MAX_RADIUS_M = 40000
MAX_RADIUS_MILES = MAX_RADIUS_M / METERS_PER_MILE
PAGE_SIZE = 50
MAX_RESULTS = 240        # Yelp's cap on offset + limit
MAX_PAGES = MAX_RESULTS // PAGE_SIZE + 1
DAILY_RESERVE = 5        # calls left untouched when Yelp's daily quota runs low
KEY_ERRORS = ("TOKEN_INVALID", "UNAUTHORIZED_API_KEY", "UNAUTHORIZED_ACCESS_TOKEN",
              "TOKEN_MISSING")
QUOTA_ERROR = "ACCESS_LIMIT_REACHED"


def grid_for(radius_miles, grid_cells):
    """The smallest grid (at least the one asked for) whose cells fit Yelp's 25-mile limit."""
    for cells in (1, 7, 19):
        if cells >= grid_cells and search_grid(0, 0, radius_miles, cells)[0][2] <= MAX_RADIUS_MILES:
            return cells
    return 19


def queries_for(keywords):
    """Word searches for the user's keywords and the competitors, then Yelp category searches."""
    words = [q for q in dict.fromkeys(list(keywords) + list(config.COMPETITORS))
             if q not in config.YELP_SEARCHES]
    return words + list(config.YELP_SEARCHES)


def estimate_requests(queries, radius_miles, grid_cells):
    """Requests for the first page of every search (what a default run spends at most)."""
    return len(queries) * len(search_grid(0, 0, radius_miles, grid_for(radius_miles, grid_cells)))


def parse_business(biz, query=""):
    coords = biz.get("coordinates") or {}
    loc = biz.get("location") or {}
    cats = [c for c in biz.get("categories") or [] if c.get("alias")]
    url = (biz.get("url") or "").split("?")[0]     # drop Yelp's tracking parameters
    return Lead(
        name=(biz.get("name") or "").strip(),
        lat=coords.get("latitude"),
        lon=coords.get("longitude"),
        source="yelp",
        source_id=biz.get("id", ""),
        address=" ".join(x for x in [loc.get("address1") or "", loc.get("address2") or ""] if x),
        city=loc.get("city") or "",
        state=loc.get("state") or "",
        zip=loc.get("zip_code") or "",
        phone=biz.get("display_phone") or biz.get("phone") or "",
        raw_categories=[f"yelp:{c['alias']}" for c in cats],
        primary_category=", ".join(c.get("title", "") for c in cats[:3]),
        yelp_reviews=biz.get("review_count"),
        business_status="CLOSED_PERMANENTLY" if biz.get("is_closed") else "OPERATIONAL",
        map_url=url,
        search_terms=[query] if query else [],
    )


def search(lat, lon, radius_miles, queries, api_key, grid_cells=1, max_requests=None,
           progress=None):
    """Return (leads, requests_made, warnings). Put the most important queries first.

    A query that names an entry of config.YELP_SEARCHES searches those Yelp
    categories (most-reviewed first); any other query is searched as words.
    """
    if not api_key:
        raise SourceError("No Yelp API key configured")
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    cells = search_grid(lat, lon, radius_miles, grid_for(radius_miles, grid_cells))
    if max_requests is None:
        max_requests = config.YELP_DEFAULT_MAX_REQUESTS
    quota = {"left": None}
    warnings = []
    if cells[0][2] > MAX_RADIUS_MILES:
        warnings.append(f"Yelp searches at most {MAX_RADIUS_MILES:.0f} miles around each point, "
                        "so parts of this very wide area were not searched by Yelp.")

    def fetch_page(query, cell, offset):
        clat, clon, crad = cell
        offset = offset or 0
        params = {"latitude": round(clat, 6), "longitude": round(clon, 6),
                  "radius": int(min(crad * METERS_PER_MILE, MAX_RADIUS_M)),
                  "limit": min(PAGE_SIZE, MAX_RESULTS - offset), "offset": offset}
        if query in config.YELP_SEARCHES:
            params.update(categories=",".join(config.YELP_SEARCHES[query]),
                          sort_by="review_count")
        else:
            params["term"] = query
        got = CaseInsensitiveDict()       # header names may arrive in any case
        try:
            data = request_json("GET", SEARCH_URL, params=params, headers=headers,
                                use_cache=False, no_retry=(QUOTA_ERROR,), response_headers=got)
        except HttpError as exc:
            if QUOTA_ERROR in str(exc):
                quota["left"] = 0
            raise
        finally:
            left = str(got.get("RateLimit-Remaining", "")).strip()
            if left.isdigit() and quota["left"] != 0:
                quota["left"] = int(left)
        items = data.get("businesses") or []
        nxt = offset + len(items)
        more = len(items) == params["limit"] and nxt < min(data.get("total") or 0, MAX_RESULTS)
        return items, (nxt if more else None)

    def stop_check():
        if quota["left"] is not None and quota["left"] <= DAILY_RESERVE:
            return (f"Yelp's daily limit is almost used up ({quota['left']} calls left; "
                    "it resets at midnight UTC)")
        return None

    leads, n, w = run_searches(
        "Yelp", queries, cells, fetch_page, parse_business,
        max_pages=MAX_PAGES, max_requests=max_requests, progress=progress,
        cache_version=["yelp-chain-v1", config.YELP_SEARCHES],
        cache_ttl=config.YELP_CACHE_TTL_SECONDS, cache_partial=True, stop_check=stop_check,
        key_errors=KEY_ERRORS)
    if any(QUOTA_ERROR in x for x in w):
        w = [x for x in w if QUOTA_ERROR not in x]
        w.append("Yelp's daily limit was reached, so some Yelp searches did not run "
                 "(it resets at midnight UTC).")
    return leads, n, warnings + w


_GOOGLE_KEY = re.compile(r"^AIza[0-9A-Za-z_\-]{30,}$")
_YELP_KEY = re.compile(r"^[A-Za-z0-9_\-]{128}$")


def looks_like_google_key(key):
    return bool(_GOOGLE_KEY.match((key or "").strip()))


def looks_like_yelp_key(key):
    return bool(_YELP_KEY.match((key or "").strip()))
