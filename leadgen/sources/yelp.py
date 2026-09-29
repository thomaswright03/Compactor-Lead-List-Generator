"""Yelp Fusion business search (needs YELP_API_KEY).

Yelp has phone numbers, addresses and review counts for most consumer-facing
businesses (grocery, retail, hotels, hospitals), but no company websites and
thin coverage of warehouses and plants, which the free map data fills in.
Yelp searches a radius of at most 40 km (~25 miles), so wider areas are
covered with the 7- or 19-cell grid.

The website may make at most config.YELP_DAILY_LIMIT Yelp calls a day in total
(see usage.py); every call, retries included, is counted before it is made.
"""

import re
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from requests.structures import CaseInsensitiveDict

from .. import config, usage
from ..geo import METERS_PER_MILE, search_grid
from ..http import HttpError, request_json
from ..models import Lead
from . import SourceError
from .paging import Cell, run_searches

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


def grid_for(radius_miles: float, grid_cells: int) -> int:
    """The smallest grid (at least the one asked for) whose cells fit Yelp's 25-mile limit."""
    for cells in (1, 7, 19):
        if cells >= grid_cells and search_grid(0, 0, radius_miles, cells)[0][2] <= MAX_RADIUS_MILES:
            return cells
    return 19


def queries_for(keywords: Iterable[str]) -> list[str]:
    """Yelp category searches (the best leads, so they come first under the daily
    limit), then word searches for the user's keywords and the competitors."""
    words = [q for q in dict.fromkeys(list(keywords) + list(config.COMPETITORS))
             if q not in config.YELP_SEARCHES]
    return list(config.YELP_SEARCHES) + words


def choose_grid(radius_miles: float, grid_cells: int, n_queries: int, affordable: int) -> int:
    """Cells to search. Past 25 miles Yelp needs 7 cells; when that is more first
    pages than the calls available and the user did not ask for a wider grid,
    one 25-mile search around the center is used instead."""
    cells = grid_for(radius_miles, grid_cells)
    if grid_cells == 1 and cells > 1 and n_queries * cells > affordable:
        return 1
    return cells


def slim(biz: dict[str, Any]) -> dict[str, Any]:
    """Only the fields parse_business reads, so cached results stay small."""
    loc = biz.get("location") or {}
    return {
        "id": biz.get("id"), "name": biz.get("name"), "url": biz.get("url"),
        "coordinates": biz.get("coordinates"), "phone": biz.get("phone"),
        "display_phone": biz.get("display_phone"), "review_count": biz.get("review_count"),
        "is_closed": biz.get("is_closed"),
        "categories": [{"alias": c.get("alias"), "title": c.get("title")}
                       for c in biz.get("categories") or []],
        "location": {k: loc.get(k) for k in ("address1", "address2", "city", "state",
                                             "zip_code")},
    }


def parse_business(biz: dict[str, Any], query: str = "") -> Lead:
    coords = biz.get("coordinates") or {}
    loc = biz.get("location") or {}
    cats = [c for c in biz.get("categories") or [] if c.get("alias")]
    url = (biz.get("url") or "").split("?")[0]     # drop Yelp's tracking parameters
    return Lead(
        name=(biz.get("name") or "").strip(),
        lat=coords.get("latitude"),              # type: ignore[arg-type] # None: dropped
        lon=coords.get("longitude"),             # type: ignore[arg-type] # by paging.py
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


def _resets(budget: usage.DailyBudget) -> str:
    reset = budget.reset_text()
    return f"; all are back by {reset}" if reset else ""


def used_up(budget: usage.DailyBudget) -> str:
    return (f"this site's {budget.limit} Yelp calls for the last 24 hours are used up"
            f"{_resets(budget)}")


def search(lat: float, lon: float, radius_miles: float, queries: Sequence[str], api_key: str,
           grid_cells: int = 1, max_requests: int | None = None,
           progress: Callable[[str], None] | None = None) -> tuple[list[Lead], int, list[str]]:
    """Return (leads, requests_made, warnings). Put the most important queries first.

    A query that names an entry of config.YELP_SEARCHES searches those Yelp
    categories (most-reviewed first); any other query is searched as words.
    Calls stop at max_requests or at the site's daily Yelp limit, whichever is lower.
    """
    if not api_key:
        raise SourceError("No Yelp API key configured")
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    budget = usage.yelp_budget()
    left = budget.left()
    cap = config.YELP_DEFAULT_MAX_REQUESTS if max_requests is None else max_requests
    if budget.problem:                # no count means no calls, and no usable cache either
        return [], 0, [f"Yelp was skipped: Yelp is paused because {budget.problem}."]
    limit_note = None
    if left == 0:
        limit_note = used_up(budget)
    elif left <= cap:                 # the daily limit, not the request cap, is what binds
        limit_note = (f"this site may make {budget.limit} Yelp calls in any 24 hours and {left} "
                      f"were left{_resets(budget)}")
    elif len(queries) * len(search_grid(0, 0, radius_miles, grid_for(radius_miles, grid_cells))) > cap:
        limit_note = (f"the request cap; up to {left} of the {budget.limit} Yelp calls for 24 hours are "
                      "left if you raise it")
    cap = min(cap, left)
    n_cells = choose_grid(radius_miles, grid_cells, len(queries), cap)
    cells = search_grid(lat, lon, min(radius_miles, MAX_RADIUS_MILES) if n_cells == 1
                        else radius_miles, n_cells)
    quota: dict[str, int | None] = {"left": None}
    warnings = []
    if progress:
        progress(f"Yelp: {len(queries)} searches x {n_cells} area(s), up to {cap} calls "
                 f"({left} of {budget.limit} left in the last 24 hours)")
    if n_cells < grid_for(radius_miles, grid_cells) and cap:
        warnings.append(f"Yelp searched the {MAX_RADIUS_MILES:.0f} miles around the center (the "
                        f"most one Yelp search reaches). Covering all {radius_miles:g} miles "
                        "takes 7 areas, more calls than this search could make.")
    elif cells[0][2] > MAX_RADIUS_MILES:
        warnings.append(f"Yelp searches at most {MAX_RADIUS_MILES:.0f} miles around each point, "
                        "so parts of this very wide area were not searched by Yelp.")

    cache = usage.SharedCache()

    def take_call() -> str | None:
        if cache.down:
            return ("Yelp stopped because saved Yelp results could not be read from the "
                    "database, and searching again would spend calls on them")
        if budget.take():
            return None
        if budget.problem:
            return f"Yelp is paused because {budget.problem}"
        return used_up(budget)

    def fetch_page(query: str, cell: Cell, offset: int | None) -> tuple[list[Any], int | None]:
        clat, clon, crad = cell
        offset = offset or 0
        params: dict[str, Any] = {"latitude": round(clat, 6), "longitude": round(clon, 6),
                  "radius": int(min(crad * METERS_PER_MILE, MAX_RADIUS_M)),
                  "limit": min(PAGE_SIZE, MAX_RESULTS - offset), "offset": offset}
        if query in config.YELP_SEARCHES:
            params.update(categories=",".join(config.YELP_SEARCHES[query]),
                          sort_by="review_count")
        else:
            params["term"] = query
        got: CaseInsensitiveDict[str] = CaseInsensitiveDict()       # header names may arrive in any case
        try:
            data = request_json("GET", SEARCH_URL, params=params, headers=headers,
                                use_cache=False, no_retry=(QUOTA_ERROR,), response_headers=got,
                                before_retry=lambda: take_call() is None)
        except HttpError as exc:
            if QUOTA_ERROR in str(exc):
                quota["left"] = 0
            raise
        finally:
            left_now = str(got.get("RateLimit-Remaining", "")).strip()
            if left_now.isdigit() and quota["left"] != 0:
                quota["left"] = int(left_now)
        items = data.get("businesses") or []
        nxt = offset + len(items)
        more = len(items) == params["limit"] and nxt < min(data.get("total") or 0, MAX_RESULTS)
        return [slim(b) for b in items], (nxt if more else None)

    def stop_check() -> str | None:
        stop = config.stop_reason("yelp")
        if stop:
            return stop
        if quota["left"] is not None and quota["left"] <= DAILY_RESERVE:
            return (f"Yelp's daily limit is almost used up ({quota['left']} calls left; "
                    "it resets at midnight UTC)")
        return take_call()

    try:
        leads, n, w = run_searches(
        "Yelp", queries, cells, fetch_page, parse_business,
        max_pages=MAX_PAGES, max_requests=cap, progress=progress,
        cache_version=["yelp-chain-v2", config.YELP_SEARCHES],
        cache_ttl=config.YELP_CACHE_TTL_SECONDS, cache_partial=True, stop_check=stop_check,
        key_errors=KEY_ERRORS, cache=cache, cap_reason=limit_note)
    finally:
        cache.close()
    if any(QUOTA_ERROR in x for x in w):
        w = [x for x in w if QUOTA_ERROR not in x]
        w.append("Yelp's daily limit was reached, so some Yelp searches did not run "
                 "(it resets at midnight UTC).")
    return leads, n, warnings + w


_GOOGLE_KEY = re.compile(r"^AIza[0-9A-Za-z_\-]{30,}$")
_YELP_KEY = re.compile(r"^[A-Za-z0-9_\-]{128}$")


def looks_like_google_key(key: str | None) -> bool:
    return bool(_GOOGLE_KEY.match((key or "").strip()))


def looks_like_yelp_key(key: str | None) -> bool:
    return bool(_YELP_KEY.match((key or "").strip()))
