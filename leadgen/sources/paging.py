"""Budgeted, paged searches shared by the paid sources (Google, Yelp).

Every (phrase, grid cell) pair is one search that can span several result
pages. The request budget is spent so that a cap trims depth, never whole
phrases: cell by cell (center first), every phrase gets page 1 before any
search gets page 2. A search's pages are cached together once complete, so a
re-run never replays a stale page token. Sources that page by offset (Yelp)
also cache unfinished searches, and a re-run continues where they stopped.
"""

import json
import time
from collections.abc import Callable, Sequence
from typing import Any, Protocol

from ..http import HttpError, cache_get, cache_put
from ..models import Lead
from ..progress import PAID, Step
from . import SourceError, report_found


class Cache(Protocol):
    """Where a source keeps its searches (usage.DbCache; the default is the file cache)."""

    def get(self, key: str, ttl: float | None = None) -> Any: ...

    def put(self, key: str, value: object, ttl: float | None = None) -> None: ...


Cell = tuple[float, float, float]           # a search circle: lat, lon, radius (miles)


def run_searches(source: str, queries: Sequence[str], cells: Sequence[Cell],
                 fetch_page: Callable[[str, Cell, Any], tuple[list[Any], Any]],
                 parse: Callable[[Any, str], Lead | None], *, max_pages: int, max_requests: int,
                 cache_version: object, progress: Callable[[str], None] | None = None,
                 token_delay: float = 0.0, key_errors: Sequence[str] = (),
                 cache_ttl: float | None = None, stop_check: Callable[[], str | None] | None = None,
                 cache_key: Callable[[str, Cell], str] | None = None, cache_partial: bool = False,
                 cache: Cache | None = None,
                 cap_reason: str | None = None) -> tuple[list[Lead], int, list[str]]:
    """Return (leads, requests_made, warnings).

    fetch_page(query, cell, token) -> (items, next_token); token is None for page 1.
    parse(item, query) -> Lead or None.
    key_errors: extra text that marks a bad key in an error message.
    stop_check() -> a reason to stop early (e.g. the daily quota is nearly spent), or None.
    cache_key(query, cell) -> the cache key of one search (default: built from cache_version).
    cache_partial: also cache unfinished searches and resume them (offset paging only).
    cache: an object with get(key, ttl) and put(key, value, ttl) (default: the file cache).
    cap_reason: why max_requests is what it is, when raising it would not help.
    """
    get: Callable[[str, float | None], Any] = cache.get if cache else cache_get
    put: Callable[[str, object], None] = ((lambda k, v: cache.put(k, v, cache_ttl)) if cache
                                          else cache_put)

    def keep(items: list[Any], query: str) -> list[Lead]:
        return [lead for lead in (parse(i, query) for i in items)
                if lead is not None and lead.lat is not None and lead.lon is not None]

    def key_of(q: str, cell: Cell) -> str:
        return (cache_key(q, cell) if cache_key
                else json.dumps([cache_version, q, *(round(x, 5) for x in cell)]))

    leads, chains, cached_q = _plan(queries, cells, key_of, lambda k: get(k, cache_ttl), keep,
                                    cache_partial, max_pages)
    report_found(leads)
    warnings: list[str] = []
    fresh = sum(1 for c in chains if c["pages"] == 0)
    if fresh > max_requests and not cap_reason:
        warnings.append(
            f"{source} request cap ({max_requests}) is below the {fresh} searches needed "
            f"({len(queries)} phrases x {len(cells)} areas); the last {fresh - max_requests} "
            "searches were skipped. Raise the request cap (--max-requests) to about "
            f"{fresh * max_pages} for full coverage.")

    rounds = _Rounds(source, fetch_page, keep, max_requests, progress, token_delay, key_errors,
                     stop_check)
    for page in range(max_pages):
        if not rounds.fetch(chains, page):
            break
    leads += rounds.leads
    warnings += rounds.warnings
    if rounds.capped:
        warnings.append(_capped_note(source, queries, chains, cached_q, rounds.stopped,
                                     max_requests, cap_reason))
    _save(chains, put, max_pages, cache_partial)
    return leads, rounds.requests_made, warnings


Chain = dict[str, Any]      # one (phrase, cell) search: its pages so far, token, state


def _plan(queries: Sequence[str], cells: Sequence[Cell], key_of: Callable[[str, Cell], str],
          get: Callable[[str], Any], keep: Callable[[list[Any], str], list[Lead]],
          cache_partial: bool, max_pages: int) -> tuple[list[Lead], list[Chain], set[str]]:
    """(leads from cached searches, the searches still to fetch, phrases found cached)."""
    leads: list[Lead] = []
    chains: list[Chain] = []
    cached_q: set[str] = set()
    for cell in cells:
        for q in queries:
            key = key_of(q, cell)
            cached = get(key)
            chain: Chain = {"q": q, "cell": cell, "key": key, "items": [], "token": None,
                            "token_at": 0.0, "pages": 0, "failed": False, "fetched": False}
            if isinstance(cached, list):          # a complete search
                cached_q.add(q)
                leads += keep(cached, q)
                continue
            if cache_partial and isinstance(cached, dict) and cached.get("items"):
                # An unfinished search: keep its pages and continue from where it stopped.
                cached_q.add(q)
                leads += keep(cached["items"], q)
                chain.update(items=cached["items"], token=cached.get("token"),
                             pages=int(cached.get("pages") or 1))
                if chain["token"] is None or chain["pages"] >= max_pages:
                    continue
            chains.append(chain)
    return leads, chains, cached_q


class _Rounds:
    """Fetches the searches' pages round by round within the request budget."""

    def __init__(self, source: str, fetch_page: Callable[[str, Cell, Any], tuple[list[Any], Any]],
                 keep: Callable[[list[Any], str], list[Lead]], max_requests: int,
                 progress: Callable[[str], None] | None, token_delay: float,
                 key_errors: Sequence[str], stop_check: Callable[[], str | None] | None) -> None:
        self.source, self.fetch_page, self.keep = source, fetch_page, keep
        self.max_requests, self.progress, self.token_delay = max_requests, progress, token_delay
        self.key_errors, self.stop_check = key_errors, stop_check
        self.leads: list[Lead] = []
        self.warnings: list[str] = []
        self.requests_made = 0
        self.capped = False
        self.stopped: str | None = None
        self.any_ok = False

    def fetch(self, chains: list[Chain], page: int) -> bool:
        """Round `page` fetches each search's (page + 1)-th page, so resumed searches wait
        until every fresh search has its first page. False once the budget stops it."""
        active = [c for c in chains if not c["failed"] and c["pages"] == page
                  and (page == 0 or c["token"] is not None)]
        for n, chain in enumerate(active, start=1):
            if self.requests_made >= self.max_requests:
                self.capped = True
                return False
            self.stopped = self.stop_check() if self.stop_check else None
            if self.stopped:
                self.capped = True
                return False
            if self.progress:
                self.progress(Step(f"{self.source} page {page + 1}: '{chain['q']}' ({n}/{len(active)})",
                                   PAID, self.requests_made + 1, self.max_requests,
                                   self.source.lower(), chain["q"]))
            self._one(chain)
        return True

    def _one(self, chain: Chain) -> None:
        if chain["token"] is not None:
            wait = self.token_delay - (time.time() - chain["token_at"])
            if wait > 0:
                time.sleep(wait)
        self.requests_made += 1
        try:
            items, token = self.fetch_page(chain["q"], chain["cell"], chain["token"])
        except HttpError as exc:
            msg = str(exc)
            bad_key = ("HTTP 401" in msg or "HTTP 403" in msg
                       or any(k in msg for k in self.key_errors))
            # A first-page 400 before anything has worked means a bad request or key.
            first_rejected = "HTTP 400" in msg and not self.any_ok and chain["token"] is None
            if bad_key or first_rejected:
                raise SourceError(f"{self.source} rejected the request. Check the API key. {msg}") from exc
            what = "a later results page" if chain["token"] is not None else "the search"
            self.warnings.append(f"{self.source} '{chain['q']}': {what} failed ({msg[:160]}); "
                                 "kept the results already found")
            chain["failed"] = True
            chain["token"] = None
            return
        self.any_ok = True
        chain["fetched"] = True
        chain["items"] += items
        chain["pages"] += 1
        chain["token"] = token
        chain["token_at"] = time.time()
        kept = self.keep(items, chain["q"])
        self.leads += kept
        report_found(kept)


def _capped_note(source: str, queries: Sequence[str], chains: list[Chain], cached_q: set[str],
                 stopped: str | None, max_requests: int, cap_reason: str | None) -> str:
    """The warning for a run the budget (or stop_check) cut short."""
    skipped = sum(1 for c in chains if c["pages"] == 0 and not c["failed"])
    more = sum(1 for c in chains if c["token"] is not None)
    tried = {c["q"] for c in chains if c["pages"] or c["failed"]} | cached_q
    never = [q for q in queries if q not in tried]
    if stopped:
        why = f"Stopped early: {stopped}"
    elif cap_reason:
        why = (f"Stopped after {max_requests} {source} calls ({cap_reason})" if max_requests
               else f"No {source} calls were made ({cap_reason})")
    else:
        why = f"Stopped at the {max_requests}-request {source} cap"
    advice = ("" if stopped or cap_reason
              else " Raise the request cap (--max-requests) for more coverage.")
    return (f"{why}: {skipped} {source} searches not run, {more} had more result "
            f"pages.{advice}" + (f" Not searched at all: {', '.join(never)}." if never else ""))


def _save(chains: list[Chain], put: Callable[[str, object], None], max_pages: int,
          cache_partial: bool) -> None:
    """Cache each search fetched this run: complete ones whole, unfinished ones (offset
    paging only) with where to continue."""
    for chain in chains:
        complete = chain["token"] is None or chain["pages"] >= max_pages
        if not chain["fetched"] or chain["failed"]:
            continue              # nothing new: leave the cached copy (and its expiry) alone
        if complete:
            put(chain["key"], chain["items"])
        elif cache_partial:
            put(chain["key"], {"items": chain["items"], "token": chain["token"],
                               "pages": chain["pages"]})
