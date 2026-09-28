"""Glue: geocode -> query sources -> radius filter -> dedupe -> score -> sort."""

import os
import time
from dataclasses import asdict, dataclass, field
from typing import Optional

from . import config
from .dedupe import dedupe
from .geo import geocode, haversine_miles
from .scoring import score_lead
from .sources import SourceError, google_places, osm

SOURCES = ("auto", "google", "osm", "both")
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
    api_key: str = ""

    def resolved_key(self):
        return self.api_key or os.environ.get("GOOGLE_PLACES_API_KEY", "")


@dataclass
class RunResult:
    leads: list
    center: tuple
    location_label: str
    warnings: list
    stats: dict

    def run_info(self, params):
        info = {k: v for k, v in asdict(params).items() if k != "api_key"}
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
    api_key = params.resolved_key()

    say = progress or (lambda msg: None)
    say(f"Locating '{params.location}'")
    lat, lon, label = geocode(params.location, api_key or None)

    use_google = params.source in ("google", "both") or (params.source == "auto" and api_key)
    use_osm = params.source in ("osm", "both", "auto")
    if params.source == "google" and not api_key:
        raise PipelineError("Source 'google' needs GOOGLE_PLACES_API_KEY (or use --source osm)")

    raw, warnings, stats, errors = [], [], {}, []
    if not api_key and params.source == "auto":
        warnings.append("No Google Places API key set: using free OpenStreetMap data only. "
                        "Add a key for much better phone/website coverage.")
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
    in_range = []
    for lead in raw:
        lead.distance_miles = round(haversine_miles(lat, lon, lead.lat, lead.lon), 2)
        if lead.distance_miles > params.radius_miles:
            continue
        in_range.append(lead)

    say("Merging duplicates")
    # Closed places are dropped after merging, so an old map copy of a place
    # Google reports closed cannot slip through on its own.
    merged = dedupe(in_range)
    n_merged = len(merged)
    if not params.include_closed:
        merged = [l for l in merged if l.business_status != "CLOSED_PERMANENTLY"]
    for lead in merged:
        lead.distance_miles = round(haversine_miles(lat, lon, lead.lat, lead.lon), 2)
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
        "results in radius": len(in_range),
        "after dedupe": n_merged,
        "leads kept": len(kept),
        "tier A": sum(l.tier == "A" for l in kept),
        "tier B": sum(l.tier == "B" for l in kept),
        "competitors flagged": sum(l.lead_type == "Competitor" for l in kept),
        "seconds": round(time.time() - started, 1),
    })
    return RunResult(kept, (lat, lon), label, warnings, stats)
