"""Glue: geocode -> query sources -> radius filter -> dedupe -> score -> sort."""

import os
import time
from dataclasses import asdict, dataclass, field

from . import config
from .dedupe import dedupe
from .geo import geocode, haversine_miles
from .scoring import score_lead
from .sources import SourceError, google_places, osm

SOURCES = ("auto", "google", "osm", "both")


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
    max_requests: int = 150
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
        # Competitor names are searched too, so they show up flagged in the list.
        queries = list(dict.fromkeys(config.GOOGLE_QUERIES + keywords + list(config.COMPETITORS)))
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
        if lead.business_status == "CLOSED_PERMANENTLY" and not params.include_closed:
            continue
        in_range.append(lead)

    say("Merging duplicates")
    merged = dedupe(in_range)
    for lead in merged:
        lead.distance_miles = round(haversine_miles(lat, lon, lead.lat, lead.lon), 2)
        score_lead(lead, keywords)

    kept = []
    for lead in merged:
        is_competitor = lead.lead_type in ("Competitor", "Own company")
        if not is_competitor:   # competitors are always kept and flagged, never filtered out
            if lead.score < params.min_score:
                continue
            if params.only_keyword_matches and not lead.matched_keywords:
                continue
        kept.append(lead)
    kept.sort(key=lambda l: (-l.score, l.distance_miles, l.name.lower()))
    if params.limit and params.limit > 0:
        kept = kept[:params.limit]

    stats.update({
        "results in radius": len(in_range),
        "after dedupe": len(merged),
        "leads kept": len(kept),
        "tier A": sum(l.tier == "A" for l in kept),
        "tier B": sum(l.tier == "B" for l in kept),
        "competitors flagged": sum(l.lead_type == "Competitor" for l in kept),
        "seconds": round(time.time() - started, 1),
    })
    return RunResult(kept, (lat, lon), label, warnings, stats)
