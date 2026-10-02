"""Glue: geocode -> query sources -> radius filter -> dedupe -> score -> sort."""

import logging
import os
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from . import config
from .dedupe import SAME_PHONE_MILES, dedupe
from .geo import geocode, haversine_miles
from .models import Lead
from .scoring import keyword_hits, score_lead
from .sources import SourceError, google_places, osm, yelp

# auto: every paid source that has a key, plus the free map data.
# both: Google plus the free map data.
SOURCES = ("auto", "google", "yelp", "osm", "both")
GRIDS = (1, 7, 19)
EXEMPT_TYPES = ("Competitor", "Own company")   # always kept and flagged, never filtered
# How each source is named on the page.
SOURCE_NAMES = {"google": "Google", "yelp": "Yelp", "osm": "the map data service (OpenStreetMap)"}

log = logging.getLogger(__name__)

# Called with each progress message of a search (the page turns them into its steps).
Progress = Callable[[str], None]


class PipelineError(RuntimeError):
    """A search that could not run. The message is plain words for the page;
    detail holds the technical reason (for the log and the command line)."""

    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.detail = detail


def _names(sources: Sequence[str]) -> str:
    names = [SOURCE_NAMES[s] for s in sources]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]


@dataclass
class SearchParams:
    location: str = config.DEFAULT_LOCATION
    radius_miles: float = config.DEFAULT_RADIUS_MILES
    keywords: list[str] = field(default_factory=lambda: list(config.DEFAULT_KEYWORDS))
    source: str = "auto"
    min_score: int = config.DEFAULT_MIN_SCORE
    grid: int = 1
    max_requests: int | None = None     # None: enough for every search and page
    only_keyword_matches: bool = False
    include_closed: bool = False
    limit: int = 0
    api_key: str = ""          # Google Places
    yelp_api_key: str = ""
    skip_yelp: bool = False    # the command line without the site's database (cli.py)
    # Where the place was already found (web/finding.py locates it before the day is
    # claimed, so the person sees it first): (lat, lon), and its label. None: run()
    # looks the location up itself.
    center: tuple[float, float] | None = None
    place: str = ""

    def resolved_keys(self) -> tuple[str, str, bool]:
        """Return (google key, yelp key, misplaced) from the params or the environment.

        A Yelp key pasted into the Google setting is recognized and never sent to
        Google; misplaced is True then (and it is used for Yelp if no Yelp key is set).
        A source switched off by the administrator (config.GOOGLE_OFF_ENV /
        YELP_OFF_ENV) gets no key, so it is never called.
        """
        google = (self.api_key or os.environ.get("GOOGLE_PLACES_API_KEY", "")).strip()
        yelp_key = (self.yelp_api_key or os.environ.get("YELP_API_KEY", "")).strip()
        misplaced = bool(google) and yelp.looks_like_yelp_key(google) and \
            not yelp.looks_like_google_key(google)
        if misplaced:
            yelp_key = yelp_key or google
            google = ""
        if config.switched_on(config.GOOGLE_OFF_ENV):
            google = ""
        if config.switched_on(config.YELP_OFF_ENV) or self.skip_yelp:
            yelp_key = ""
        return google, yelp_key, misplaced


@dataclass
class RunResult:
    leads: list[Lead]
    center: tuple[float, float]
    location_label: str
    warnings: list[str]
    stats: dict[str, object]
    problems: list[str] = field(default_factory=list)   # technical detail of failed sources
    # The sources that failed ("google", "yelp", "osm") while others still found businesses.
    failed_sources: list[str] = field(default_factory=list)
    # Of those, the ones that answered for part of the area (the map data, asked in parts).
    partial_sources: list[str] = field(default_factory=list)
    # The map-data parts no server answered for (osm.PartialResult.missing), and how many
    # areas the map search was cut into: the site asks them again in the background (fillin.py).
    osm_missing: list[osm.Part] = field(default_factory=list)
    osm_areas: int = 0
    # Why the search was cut short ("the administrator paused searching"), or "".
    stopped: str = ""

    def run_info(self, params: SearchParams) -> dict[str, Any]:
        info = {k: v for k, v in asdict(params).items()
                if k not in ("api_key", "yelp_api_key", "skip_yelp", "center", "place")}
        info["keywords"] = ", ".join(params.keywords)
        info["resolved location"] = f"{self.location_label} ({self.center[0]:.4f}, {self.center[1]:.4f})"
        info.update(self.stats)
        if self.warnings:
            info["warnings"] = " | ".join(self.warnings)
        return info


def run(params: SearchParams, progress: Progress | None = None) -> RunResult:
    started = time.time()
    keywords = [k.strip() for k in params.keywords if k and k.strip()]
    api_key, yelp_key, misplaced = params.resolved_keys()
    use = _sources_to_use(params, api_key, yelp_key, misplaced)

    say = progress or (lambda msg: None)
    say(f"Locating '{params.location}'")
    if params.center is not None:
        (lat, lon), label = params.center, params.place or params.location
    else:
        lat, lon, label = geocode(params.location, api_key)

    warnings = _key_warnings(params, api_key, yelp_key, misplaced, use[1])
    stats: dict[str, object] = {}
    missing: dict[str, Any] = {}
    raw, errors, failed, partly = _query_sources(params, use, (api_key, yelp_key),
                                                 keywords, lat, lon, progress, warnings, stats,
                                                 missing)
    stopped = missing.get("stopped", "")

    say(f"Filtering {len(raw)} raw results to {params.radius_miles:g} miles")
    kept = finish_leads(raw, (lat, lon), params, keywords, stats, say)
    stats["seconds"] = round(time.time() - started, 1)
    return RunResult(kept, (lat, lon), label, warnings, stats, problems=errors,
                     failed_sources=failed, partial_sources=partly,
                     osm_missing=missing.get("parts", []), osm_areas=missing.get("areas", 0),
                     stopped=stopped)


def locate(params: SearchParams) -> tuple[float, float, str]:
    """Where the search will run: (lat, lon, label) for its location, looked up with
    the Google key when there is one (geo.geocode). Raises GeocodeError."""
    return geocode(params.location, params.resolved_keys()[0])


def finish_leads(raw: list[Lead], centre: tuple[float, float], params: SearchParams,
                 keywords: list[str], stats: dict[str, object] | None = None,
                 say: Progress | None = None) -> list[Lead]:
    """The sources' listings as the search's leads, best first: within the radius,
    duplicates merged, scored, and the ones left out (closed, low score, not matching,
    over the limit) counted in stats. Also used for the businesses the map areas a
    search missed find later (fillin.py)."""
    lat, lon = centre
    say = say or (lambda msg: None)
    stats = stats if stats is not None else {}
    # Keep a margin while merging, so a listing just outside the radius can still
    # merge with (and e.g. mark closed) its copy just inside; the exact radius
    # is applied afterwards.
    margin = params.radius_miles + SAME_PHONE_MILES
    near = [l for l in raw if haversine_miles(lat, lon, l.lat, l.lon) <= margin]
    n_in_radius = sum(haversine_miles(lat, lon, l.lat, l.lon) <= params.radius_miles for l in near)

    say("Merging duplicates")
    merged = _merge_and_score(near, (lat, lon), params, keywords)
    n_merged = len(merged)
    closed = sum(l.business_status == "CLOSED_PERMANENTLY" for l in merged)
    # Closed places are dropped after merging, so an old map copy of a place
    # Google or Yelp reports closed cannot slip through on its own.
    if not params.include_closed:
        merged = [l for l in merged if l.business_status != "CLOSED_PERMANENTLY"]
    kept, low_score, no_match = _keep(merged, params)
    open_kept = [l for l in kept if l.business_status != "CLOSED_PERMANENTLY"]
    over_limit = n_merged - closed - low_score - no_match - len(open_kept)

    stats.update({
        "results in radius": n_in_radius,
        "duplicates merged": max(0, n_in_radius - n_merged),
        "after dedupe": n_merged,
        "closed": closed,
        "min score": params.min_score,
        "below min score": low_score,
        "not matching keywords": no_match,
        "over limit": over_limit,
        "competitors flagged": sum(l.lead_type == "Competitor" for l in open_kept),
        "leads kept": len(open_kept),
        **{f"tier {t}": sum(l.tier == t for l in open_kept) for t in "ABCD"},
    })
    return kept


def _sources_to_use(params: SearchParams, api_key: str, yelp_key: str,
                    misplaced: bool) -> tuple[bool, bool, bool]:
    """(use Google, use Yelp, use the map data); raises PipelineError for settings
    that can't be searched."""
    if params.source not in SOURCES:
        raise PipelineError(f"Unknown source '{params.source}'. Use one of {', '.join(SOURCES)}")
    if not 0 < params.radius_miles <= 100:
        raise PipelineError("Radius must be between 0 and 100 miles")
    if params.grid not in GRIDS:
        raise PipelineError(f"Grid must be one of {', '.join(map(str, GRIDS))}")
    if params.max_requests is not None and params.max_requests < 1:
        raise PipelineError("max_requests must be at least 1")
    off = [name for name, env, picked in (
        ("google", config.GOOGLE_OFF_ENV, ("google", "both")),
        ("yelp", config.YELP_OFF_ENV, ("yelp",)))
        if params.source in picked and config.switched_on(env)]
    if off:
        raise PipelineError(f"{SOURCE_NAMES[off[0]]} searches are switched off by the "
                            "administrator. Search all available sources or the free map "
                            "data instead.")
    if params.source in ("google", "both") and not api_key:
        if misplaced:
            raise PipelineError("GOOGLE_PLACES_API_KEY holds a Yelp key, not a Google key. "
                                "Put it in YELP_API_KEY and use source 'auto' or 'yelp'.")
        raise PipelineError(f"Source '{params.source}' needs GOOGLE_PLACES_API_KEY "
                            "(or use --source osm)")
    if params.source == "yelp" and not yelp_key:
        raise PipelineError("Source 'yelp' needs YELP_API_KEY (or use --source osm)")
    return (params.source in ("google", "both") or (params.source == "auto" and bool(api_key)),
            params.source == "yelp" or (params.source == "auto" and bool(yelp_key)),
            params.source in ("osm", "both", "auto"))


def _key_warnings(params: SearchParams, api_key: str, yelp_key: str, misplaced: bool,
                  use_yelp: bool) -> list[str]:
    warnings = []
    if misplaced:
        used = ("it was used for Yelp" if use_yelp and not
                (params.yelp_api_key or os.environ.get("YELP_API_KEY", "")).strip()
                else "it was not used")
        warnings.append(f"The Google key setting holds a Yelp key; {used}. "
                        "Move it to YELP_API_KEY.")
    if params.source == "auto" and not (api_key or yelp_key):
        warnings.append("No Google Places or Yelp API key set: using free OpenStreetMap data "
                        "only. Add a key for much better phone coverage.")
    return warnings


def _merge_and_score(near: list[Lead], centre: tuple[float, float], params: SearchParams,
                     keywords: list[str]) -> list[Lead]:
    """The listings merged into businesses within the radius, each with its distance,
    score and the typed search words it matches."""
    lat, lon = centre
    merged = dedupe(near)
    miles = {id(lead): round(haversine_miles(lat, lon, lead.lat, lead.lon), 2) for lead in merged}
    for lead in merged:
        lead.distance_miles = miles[id(lead)]
    merged = [l for l in merged if miles[id(l)] <= params.radius_miles]
    # Scores always use the standard words (config.DEFAULT_KEYWORDS), like the saved
    # list, so the minimum score is applied to the score that is saved and shown. The
    # words typed for this search choose what is searched for and, with
    # only_keyword_matches, which businesses are kept.
    for lead in merged:
        score_lead(lead, config.DEFAULT_KEYWORDS)
        lead.matched_keywords = keyword_hits(lead, keywords)
    return merged


def _keep(merged: list[Lead], params: SearchParams) -> tuple[list[Lead], int, int]:
    """(the leads kept, best first; how many open ones were left out for a low score;
    how many for not matching the search words). Each business within the radius ends
    up in exactly one group, so the search's numbers add up: closed, low score, not
    matching, over the lead limit, or kept."""
    low_score = no_match = 0
    kept = []
    for lead in merged:
        if lead.lead_type not in EXEMPT_TYPES:
            if lead.score < params.min_score:
                low_score += lead.business_status != "CLOSED_PERMANENTLY"
                continue
            if params.only_keyword_matches and not lead.matched_keywords:
                no_match += lead.business_status != "CLOSED_PERMANENTLY"
                continue
        kept.append(lead)

    def sort_key(l: Lead) -> tuple[int, float, str]:
        return (-l.score, l.distance_miles or 0.0, l.name.lower())

    kept.sort(key=sort_key)
    if params.limit and params.limit > 0:
        prospects = [l for l in kept if l.lead_type not in EXEMPT_TYPES][:params.limit]
        kept = sorted(prospects + [l for l in kept if l.lead_type in EXEMPT_TYPES], key=sort_key)
    return kept, low_score, no_match


class _Found:
    """What the sources answered: raw leads, technical errors, the sources that failed,
    those of them that failed for only part of the area, and plain notes and counts."""

    def __init__(self, warnings: list[str], stats: dict[str, object]) -> None:
        self.raw: list[Lead] = []
        self.errors: list[str] = []
        self.failed: list[str] = []
        self.partly: list[str] = []      # failed for only part of the area (some businesses found)
        self.warnings, self.stats = warnings, stats

    def add(self, source: str, found: list[Lead], notes: list[str], n_requests: int | None = None) -> None:
        self.raw += found
        self.warnings += notes
        if n_requests is not None:
            self.stats[f"{source} requests"] = n_requests
        self.stats[f"{source} raw results"] = len(found)

    def fail(self, source: str, exc: Exception, partly: bool = False) -> None:
        self.errors.append(str(exc))
        self.failed.append(source)
        if partly:
            self.partly.append(source)


def _query_sources(params: SearchParams, use: tuple[bool, bool, bool], keys: tuple[str, str],
                   keywords: list[str], lat: float, lon: float, progress: Progress | None,
                   warnings: list[str], stats: dict[str, object],
                   missing: dict[str, Any] | None = None
                   ) -> tuple[list[Lead], list[str], list[str], list[str]]:
    """Ask each source in turn; returns (raw leads, technical errors, the sources that
    failed, those of them that failed for only part of the area). Adds plain notes to
    warnings, counts to stats and (in missing: "parts", "areas") the map-data parts no
    server answered for. A source the administrator switched off since the search
    started is skipped, and pausing searching also stops the map data partway (in
    missing: "stopped", why); raises PipelineError when nothing at all was found."""
    use_google, use_yelp, use_osm = use
    api_key, yelp_key = keys
    got = _Found(warnings, stats)
    stopped: list[str] = []
    why: list[str] = []                      # why the search was cut short
    missing = missing if missing is not None else {}

    def halted(source: str) -> bool:
        """True when the administrator switched searching (or this source) off
        since the search started; the source is then skipped."""
        reason = config.stop_reason(source)
        if reason:
            stopped.append(source)
            why.append(reason)
            return True
        return False

    if use_google and not halted("google"):
        _ask_google(params, keywords, api_key, (lat, lon), progress, got)
    if use_yelp and not halted("yelp"):
        try:
            found, n_requests, w = yelp.search(
                lat, lon, params.radius_miles, yelp.queries_for(keywords), yelp_key, params.grid,
                params.max_requests, progress)
            got.add("yelp", found, w, n_requests)
        except SourceError as exc:
            got.fail("yelp", exc)
    if use_osm and not halted("osm"):
        try:
            found, w = osm.search(lat, lon, params.radius_miles, keywords, progress, stats=stats,
                                  stop=lambda: config.stop_reason("osm"))
            got.add("osm", found, w)
        except osm.Stopped as exc:
            # Paused partway: what the areas that had answered found is kept.
            got.add("osm", exc.leads, exc.warnings)
            why.append(exc.reason)
            if not got.raw:
                raise PipelineError("The search was stopped by the administrator (searching "
                                    "was paused) before it found anything.") from None
            got.warnings.append(f"The search was stopped by the administrator partway through "
                                f"the free map data ({exc.reason}); the businesses already "
                                "found were kept, and the rest of the area wasn't searched.")
        except osm.PartialResult as exc:
            # Some parts of the area answered: keep what they found; the search is incomplete.
            got.add("osm", exc.leads, exc.warnings)
            if exc.coverage:
                stats["osm areas searched"] = exc.coverage
            if missing is not None:
                missing.update(parts=exc.missing, areas=exc.areas)
            got.fail("osm", exc, partly=True)
        except SourceError as exc:
            got.fail("osm", exc)
    # A paid source stopped partway by a switch (sources/paging.py says why).
    why += [m.group(1) for m in (re.match(r"Stopped early: (the administrator [^:;]+)", w)
                                 for w in got.warnings) if m]
    _check_found(got, stopped)
    # The search as a whole was cut short only by the pause (a paid source switched
    # off just leaves that source out).
    paused = [w for w in why if "paused" in w]
    if paused:
        missing["stopped"] = paused[0]
    return got.raw, got.errors, got.failed, got.partly


def _ask_google(params: SearchParams, keywords: list[str], api_key: str, centre: tuple[float, float],
                progress: Progress | None, got: _Found) -> None:
    # Most specific first, so a request cap trims generic phrases, never the user's
    # keywords or the competitor names (searched so they show up flagged).
    queries = list(dict.fromkeys(keywords + list(config.COMPETITORS) + config.GOOGLE_QUERIES))
    cap = params.max_requests or google_places.estimate_requests(queries, params.grid)
    if progress:
        progress(f"Google: {len(queries)} phrases x {params.grid} area(s), up to {cap} requests")
    try:
        found, n_requests, w = google_places.search(
            centre[0], centre[1], params.radius_miles, queries, api_key, params.grid,
            params.max_requests, progress)
        got.add("google", found, w, n_requests)
    except SourceError as exc:
        got.fail("google", exc)


def _check_found(got: _Found, stopped: list[str]) -> None:
    """Log what failed, and raise PipelineError when nothing at all was found; else
    add the plain notes for sources stopped, missing or incomplete."""
    for error in got.errors:
        log.warning("Search source failed: %s", error)
    if stopped:
        log.warning("Search stopped by the administrator's switch before: %s", ", ".join(stopped))
        if not got.raw:
            raise PipelineError("The search was stopped by the administrator before it found "
                                "anything.")
        got.warnings.append(f"The search was stopped by the administrator before "
                            f"{_names(stopped)} was searched; the businesses already found were kept.")
    if got.errors and not got.raw:
        raise PipelineError(f"Couldn't reach {_names(got.failed)}, so no leads were found.",
                            detail=" | ".join(got.errors))
    got.warnings += [
        f"{SOURCE_NAMES[s][0].upper()}{SOURCE_NAMES[s][1:]} answered for only part of "
        "the area this time, so some of its businesses are missing from this search."
        if s in got.partly else
        f"Couldn't reach {SOURCE_NAMES[s]} this time, so its businesses are missing "
        "from this search." for s in got.failed]
