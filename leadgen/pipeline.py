"""Glue: geocode -> query sources -> radius filter -> dedupe -> score -> sort."""

import os
import time
from dataclasses import asdict, dataclass, field
from typing import Optional

from . import config
from .dedupe import SAME_PHONE_MILES, dedupe
from .geo import geocode, haversine_miles
from .scoring import score_lead
from .sources import SourceError, google_places, osm, yelp

# auto: every paid source that has a key, plus the free map data.
# both: Google plus the free map data.
SOURCES = ("auto", "google", "yelp", "osm", "both")
GRIDS = (1, 7, 19)
EXEMPT_TYPES = ("Competitor", "Own company")   # always kept and flagged, never filtered


class PipelineError(RuntimeError):
    pass


@dataclass
class SearchParams:
    location: str = config.DEFAULT_LOCATION
    radius_miles: float = config.DEFAULT_RADIUS_MILES
    keywords: list = field(default_factory=lambda: list(config.DEFAULT_KEYWORDS))
    source: str = "auto"
    min_score: int = config.DEFAULT_MIN_SCORE
    grid: int = 1
    max_requests: Optional[int] = None     # None: enough for every search and page
    only_keyword_matches: bool = False
    include_closed: bool = False
    limit: int = 0
    api_key: str = ""          # Google Places
    yelp_api_key: str = ""

    def resolved_key(self):
        return self.resolved_keys()[0]

    def resolved_keys(self):
        """Return (google key, yelp key, misplaced) from the params or the environment.

        A Yelp key pasted into the Google setting is recognized and never sent to
        Google; misplaced is True then (and it is used for Yelp if no Yelp key is set).
        """
        google = (self.api_key or os.environ.get("GOOGLE_PLACES_API_KEY", "")).strip()
        yelp_key = (self.yelp_api_key or os.environ.get("YELP_API_KEY", "")).strip()
        misplaced = bool(google) and yelp.looks_like_yelp_key(google) and \
            not yelp.looks_like_google_key(google)
        if misplaced:
            yelp_key = yelp_key or google
            google = ""
        return google, yelp_key, misplaced


@dataclass
class RunResult:
    leads: list
    center: tuple
    location_label: str
    warnings: list
    stats: dict

    def run_info(self, params):
        info = {k: v for k, v in asdict(params).items() if k not in ("api_key", "yelp_api_key")}
        info["keywords"] = ", ".join(params.keywords)
        info["resolved location"] = f"{self.location_label} ({self.center[0]:.4f}, {self.center[1]:.4f})"
        info.update(self.stats)
        if self.warnings:
            info["warnings"] = " | ".join(self.warnings)
        return info


def run(params: SearchParams, progress=None):
    started = time.time()
    if params.source not in SOURCES:
        raise PipelineError(f"Unknown source '{params.source}'. Use one of {', '.join(SOURCES)}")
    if not 0 < params.radius_miles <= 100:
        raise PipelineError("Radius must be between 0 and 100 miles")
    if params.grid not in GRIDS:
        raise PipelineError(f"Grid must be one of {', '.join(map(str, GRIDS))}")
    if params.max_requests is not None and params.max_requests < 1:
        raise PipelineError("max_requests must be at least 1")
    keywords = [k.strip() for k in params.keywords if k and k.strip()]
    api_key, yelp_key, misplaced = params.resolved_keys()

    use_google = params.source in ("google", "both") or (params.source == "auto" and api_key)
    use_yelp = params.source == "yelp" or (params.source == "auto" and yelp_key)
    use_osm = params.source in ("osm", "both", "auto")
    if params.source in ("google", "both") and not api_key:
        if misplaced:
            raise PipelineError("GOOGLE_PLACES_API_KEY holds a Yelp key, not a Google key. "
                                "Put it in YELP_API_KEY and use source 'auto' or 'yelp'.")
        raise PipelineError(f"Source '{params.source}' needs GOOGLE_PLACES_API_KEY "
                            "(or use --source osm)")
    if params.source == "yelp" and not yelp_key:
        raise PipelineError("Source 'yelp' needs YELP_API_KEY (or use --source osm)")

    say = progress or (lambda msg: None)
    say(f"Locating '{params.location}'")
    lat, lon, label = geocode(params.location, api_key)

    raw, warnings, stats, errors = [], [], {}, []
    if misplaced:
        used = ("it was used for Yelp" if use_yelp and not
                (params.yelp_api_key or os.environ.get("YELP_API_KEY", "")).strip()
                else "it was not used")
        warnings.append(f"The Google key setting holds a Yelp key; {used}. "
                        "Move it to YELP_API_KEY.")
    if params.source == "auto" and not (api_key or yelp_key):
        warnings.append("No Google Places or Yelp API key set: using free OpenStreetMap data "
                        "only. Add a key for much better phone coverage.")
    if use_google:
        # Most specific first, so a request cap trims generic phrases, never the user's
        # keywords or the competitor names (searched so they show up flagged).
        queries = list(dict.fromkeys(keywords + list(config.COMPETITORS) + config.GOOGLE_QUERIES))
        cap = params.max_requests or google_places.estimate_requests(queries, params.grid)
        say(f"Google: {len(queries)} phrases x {params.grid} area(s), up to {cap} requests")
        try:
            found, n_requests, w = google_places.search(
                lat, lon, params.radius_miles, queries, api_key, params.grid,
                params.max_requests, progress)
            raw += found
            warnings += w
            stats["google requests"] = n_requests
            stats["google raw results"] = len(found)
        except SourceError as exc:
            errors.append(str(exc))
    if use_yelp:
        queries = yelp.queries_for(keywords)
        try:
            found, n_requests, w = yelp.search(
                lat, lon, params.radius_miles, queries, yelp_key, params.grid,
                params.max_requests, progress)
            raw += found
            warnings += w
            stats["yelp requests"] = n_requests
            stats["yelp raw results"] = len(found)
        except SourceError as exc:
            errors.append(str(exc))
    if use_osm:
        try:
            found, w = osm.search(lat, lon, params.radius_miles, keywords, progress)
            raw += found
            warnings += w
            stats["osm raw results"] = len(found)
        except SourceError as exc:
            errors.append(str(exc))

    if errors and not raw:
        raise PipelineError("No data source succeeded: " + " | ".join(errors))
    warnings += errors

    say(f"Filtering {len(raw)} raw results to {params.radius_miles:g} miles")
    # Keep a margin while merging, so a listing just outside the radius can still
    # merge with (and e.g. mark closed) its copy just inside; the exact radius
    # is applied afterwards.
    margin = params.radius_miles + SAME_PHONE_MILES
    near = [l for l in raw if haversine_miles(lat, lon, l.lat, l.lon) <= margin]
    n_in_radius = sum(haversine_miles(lat, lon, l.lat, l.lon) <= params.radius_miles for l in near)

    say("Merging duplicates")
    merged = dedupe(near)
    for lead in merged:
        lead.distance_miles = round(haversine_miles(lat, lon, lead.lat, lead.lon), 2)
    merged = [l for l in merged if l.distance_miles <= params.radius_miles]
    n_merged = len(merged)
    # Closed places are dropped after merging, so an old map copy of a place
    # Google or Yelp reports closed cannot slip through on its own.
    if not params.include_closed:
        merged = [l for l in merged if l.business_status != "CLOSED_PERMANENTLY"]
    for lead in merged:
        score_lead(lead, keywords)

    kept = []
    for lead in merged:
        if lead.lead_type not in EXEMPT_TYPES:
            if lead.score < params.min_score:
                continue
            if params.only_keyword_matches and not lead.matched_keywords:
                continue
        kept.append(lead)
    def sort_key(l):
        return (-l.score, l.distance_miles, l.name.lower())

    kept.sort(key=sort_key)
    if params.limit and params.limit > 0:
        prospects = [l for l in kept if l.lead_type not in EXEMPT_TYPES][:params.limit]
        kept = sorted(prospects + [l for l in kept if l.lead_type in EXEMPT_TYPES], key=sort_key)

    stats.update({
        "results in radius": n_in_radius,
        "after dedupe": n_merged,
        "leads kept": len(kept),
        "tier A": sum(l.tier == "A" for l in kept),
        "tier B": sum(l.tier == "B" for l in kept),
        "competitors flagged": sum(l.lead_type == "Competitor" for l in kept),
        "seconds": round(time.time() - started, 1),
    })
    return RunResult(kept, (lat, lon), label, warnings, stats)
