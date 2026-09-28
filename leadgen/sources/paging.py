"""Budgeted, paged searches shared by the paid sources (Google, Yelp).

Every (phrase, grid cell) pair is one search that can span several result
pages. The request budget is spent so that a cap trims depth, never whole
phrases: cell by cell (center first), every phrase gets page 1 before any
search gets page 2. A search's pages are cached together only once complete,
so a re-run never replays a stale page token.
"""

import json
import time

from ..http import HttpError, cache_get, cache_put
from . import SourceError


def run_searches(source, queries, cells, fetch_page, parse, *, max_pages, max_requests,
                 cache_version, progress=None, token_delay=0.0, key_errors=(),
                 cache_ttl=None, stop_check=None):
    """Return (leads, requests_made, warnings).

    fetch_page(query, cell, token) -> (items, next_token); token is None for page 1.
    parse(item, query) -> Lead or None.
    key_errors: extra text that marks a bad key in an error message.
    stop_check() -> a reason to stop early (e.g. the daily quota is nearly spent), or None.
    """
    leads, warnings, requests_made = [], [], 0

    def keep(items, query):
        return [lead for lead in (parse(i, query) for i in items)
                if lead is not None and lead.lat is not None and lead.lon is not None]

    chains, cached_q = [], set()
    for cell in cells:
        for q in queries:
            key = json.dumps([cache_version, q] + [round(x, 5) for x in cell])
            cached = cache_get(key, cache_ttl)
            if cached is not None:
                cached_q.add(q)
                leads += keep(cached, q)
                continue
            chains.append({"q": q, "cell": cell, "key": key, "items": [], "token": None,
                           "token_at": 0.0, "pages": 0, "failed": False})

    if len(chains) > max_requests:
        warnings.append(
            f"{source} request cap ({max_requests}) is below the {len(chains)} searches needed "
            f"({len(queries)} phrases x {len(cells)} areas); the last {len(chains) - max_requests} "
            f"searches were skipped. Raise --max-requests to about {len(chains) * max_pages} "
            "for full coverage.")

    capped = False
    stopped = None
    any_ok = False
    for page in range(max_pages):
        active = [c for c in chains if not c["failed"] and (page == 0 or c["token"] is not None)]
        for n, chain in enumerate(active, start=1):
            if requests_made >= max_requests:
                capped = True
                break
            stopped = stop_check() if stop_check else None
            if stopped:
                capped = True
                break
            if progress:
                progress(f"{source} page {page + 1}: '{chain['q']}' ({n}/{len(active)})")
            if chain["token"] is not None:
                wait = token_delay - (time.time() - chain["token_at"])
                if wait > 0:
                    time.sleep(wait)
            requests_made += 1
            try:
                items, token = fetch_page(chain["q"], chain["cell"], chain["token"])
            except HttpError as exc:
                msg = str(exc)
                bad_key = ("HTTP 401" in msg or "HTTP 403" in msg
                           or any(k in msg for k in key_errors))
                # A first-page 400 before anything has worked means a bad request or key.
                first_rejected = "HTTP 400" in msg and not any_ok and chain["token"] is None
                if bad_key or first_rejected:
                    raise SourceError(f"{source} rejected the request. Check the API key. {msg}")
                what = "a later results page" if chain["token"] is not None else "the search"
                warnings.append(f"{source} '{chain['q']}': {what} failed ({msg[:160]}); "
                                "kept the results already found")
                chain["failed"] = True
                chain["token"] = None
                continue
            any_ok = True
            chain["items"] += items
            chain["pages"] += 1
            chain["token"] = token
            chain["token_at"] = time.time()
            leads += keep(items, chain["q"])
        if capped:
            break

    if capped:
        skipped = sum(1 for c in chains if c["pages"] == 0 and not c["failed"])
        more = sum(1 for c in chains if c["token"] is not None)
        tried = {c["q"] for c in chains if c["pages"] or c["failed"]} | cached_q
        never = [q for q in queries if q not in tried]
        why = (f"Stopped early: {stopped}" if stopped
               else f"Stopped at the {max_requests}-request {source} cap")
        advice = "" if stopped else " Raise --max-requests for more coverage."
        warnings.append(f"{why}: {skipped} {source} searches not run, {more} had more result "
                        f"pages.{advice}"
                        + (f" Not searched at all: {', '.join(never)}." if never else ""))

    for chain in chains:
        complete = chain["token"] is None or chain["pages"] >= max_pages
        if chain["pages"] and complete and not chain["failed"]:
            cache_put(chain["key"], chain["items"])
    return leads, requests_made, warnings
