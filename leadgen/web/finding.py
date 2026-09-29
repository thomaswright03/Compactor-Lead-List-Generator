"""The Find leads page's server side: start today's search, follow it, its history."""

import logging
import math
import re
import threading
import time
import uuid
from dataclasses import replace

from flask import Blueprint, abort, jsonify, request

from .. import config, daily, saved, store
from ..geo import GeocodeError
from ..localtime import clock_text
from ..pipeline import GRIDS, SOURCES, PipelineError, SearchParams, run
from .common import (
    NOT_USED_UP,
    SEARCH_PAUSED,
    LoadError,
    db_message,
    lead_json,
    load_saved,
    same_origin,
    state,
    switches,
    yelp_quota,
)

log = logging.getLogger("leadgen.web")

MAX_JOBS = 20
MAX_KEYWORDS = 20
MAX_KEYWORD_LEN = 60
MAX_REQUESTS_LIMIT = 5000

STEPS = ["Find the location", "Search Yelp and Google", "Search map data", "Merge and score",
         "Save"]
# A step running longer than this (seconds) is "taking longer than usual" on the page.
SLOW_SECONDS = [30, 240, 75, 60, 60]

# The search's numbers, as the page names them (the search history's "Details").
DETAIL_LABELS = {
    "google requests": "Google lookups",
    "google raw results": "Businesses from Google",
    "yelp requests": "Yelp calls",
    "yelp raw results": "Businesses from Yelp",
    "osm raw results": "Businesses from the free map data",
    "results in radius": "Within the radius",
    "after dedupe": "After merging duplicates",
    "leads kept": "Leads kept",
    "tier A": "Tier A leads",
    "tier B": "Tier B leads",
    "competitors flagged": "Competitors (flagged)",
    "seconds": "Took",
}

bp = Blueprint("finding", __name__)

_TECHNICAL = re.compile(r"https?://|HTTP \d{3}|Error\b|Exception|<html|\(\w+Error", re.I)
_SOURCE_WORDS = (("Google", "Google"), ("Yelp", "Yelp"), ("OpenStreetMap", "The map data service"),
                 ("Overpass", "The map data service"))


def plain_warning(text):
    """A search note in words for sales staff: no URLs, error names or cap settings.
    The original goes to the log."""
    who = next((name for word, name in _SOURCE_WORDS if word.lower() in text.lower()), "")
    if "the administrator" in text and text.startswith("Stopped early"):
        log.info("Search note: %s", text)
        return (f"{who or 'A source'} was stopped partway by the administrator; the businesses "
                "already found were kept.")
    if _TECHNICAL.search(text):
        log.info("Search note: %s", text)
        return (f"{who or 'One of the sources'} had a problem with part of the search; "
                "the businesses already found were kept.")
    if "request cap" in text or "--max-requests" in text:
        log.info("Search note: %s", text)
        return (f"{who or 'A paid source'} stopped at the limit on paid lookups for one "
                "search, so some searches were skipped.")
    if "No Google Places or Yelp API key" in text:
        return ("Google and Yelp aren't set up, so only the free map data was searched "
                "(it has fewer phone numbers).")
    return text


def plain_details(details):
    """A search's numbers with plain labels (older records keep the old names)."""
    out = {}
    for key, value in (details or {}).items():
        label = DETAIL_LABELS.get(key, key)
        if key == "seconds" and isinstance(value, (int, float)):
            value = f"{round(value)} seconds" if value < 120 else f"{round(value / 60)} minutes"
        out[label] = value
    return out


def plain_progress(msg):
    """The search's progress message in the page's words."""
    page = re.match(r"(Yelp|Google) page \d+: '(.*)' \((\d+)/(\d+)\)", msg)
    if page:
        return f"Searching {page.group(1)} for {page.group(2)} ({page.group(3)} of {page.group(4)})"
    if msg.startswith("Locating"):
        return "Finding the location"
    if msg.startswith(("Yelp: ", "Google: ")):
        return f"Searching {msg.split(':')[0]}"
    if msg.startswith("OpenStreetMap"):
        server = re.search(r"server (\d+) of (\d+)", msg)
        return ("Searching the free map data"
                + (f" (server {server.group(1)} of {server.group(2)})" if server else "") + "…")
    if msg.startswith("Filtering"):
        return "Keeping the businesses within the radius"
    if msg.startswith("Merging"):
        return "Merging duplicates and scoring"
    return msg


class _Progress:
    """Turns the search's progress messages into a step and a percentage for the page.

    The Yelp / Google part advances with each call; the map data comes back in one
    slow request, so its share of the bar fills gradually while it runs.
    """

    def __init__(self, job):
        self.job = job
        self.calls, self.cap, self.map_started = 0, None, None
        self.step_started = time.time()
        job.update(step=0, pct=1.0, started=time.time())

    def slow(self):
        step = self.job["step"]
        return step < len(SLOW_SECONDS) and time.time() - self.step_started > SLOW_SECONDS[step]

    def __call__(self, msg):
        job = self.job
        job["message"] = plain_progress(msg)
        if msg.startswith("Locating"):
            self._at(0, 3)
        elif re.match(r"(Yelp|Google): ", msg):
            cap = re.search(r"up to (\d+)", msg)
            self.cap = (self.cap or 0) + (int(cap.group(1)) if cap else 0)
            self._at(1, 6)
        elif re.match(r"(Yelp|Google) page \d", msg):
            self.calls += 1
            self._at(1, 6 + 42 * min(1.0, self.calls / max(self.cap or 1, 1)))
        elif msg.startswith("OpenStreetMap"):
            self.map_started = self.map_started or time.time()
            self._at(2, 50)
        elif msg.startswith("Filtering"):
            self._at(3, 88)
        elif msg.startswith("Merging"):
            self._at(3, 92)
        elif msg.startswith("Saving"):
            self._at(4, 96)

    def _at(self, step, pct):
        if step > self.job["step"]:
            self.step_started = time.time()
        self.job["step"] = max(self.job["step"], step)
        self.job["pct"] = max(self.job["pct"], pct)

    def pct(self):
        if self.job["step"] == 2 and self.map_started:
            # Creeps toward 86% over the few minutes the map servers take.
            return max(self.job["pct"],
                       50 + 36 * (1 - math.exp(-(time.time() - self.map_started) / 150)))
        return self.job["pct"]


def skipped_steps(params):
    """Steps a search won't run: Yelp and Google when neither is set up (or both are
    switched off), map data when a paid source alone was picked."""
    google, yelp_key, _ = params.resolved_keys()
    paid = (params.source in ("google", "both", "yelp")
            or (params.source == "auto" and (google or yelp_key)))
    return ([] if paid else [1]) + ([] if params.source in ("osm", "both", "auto") else [2])


def _number(form, name, default, cast, lo, hi, label):
    raw = (form.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{label} must be a number") from None
    if cast is int:
        if not math.isfinite(value) or not value.is_integer():
            raise ValueError(f"{label} must be a whole number")
        value = int(value)
    if not lo <= value <= hi:
        raise ValueError(f"{label} must be between {lo} and {hi}")
    return value


def parse_form(form):
    """Validate the form strictly; raise ValueError with a message for the page."""
    keywords = [k.strip() for k in form.get("keywords", "").split(",") if k.strip()]
    if len(keywords) > MAX_KEYWORDS or any(len(k) > MAX_KEYWORD_LEN for k in keywords):
        raise ValueError(f"Use at most {MAX_KEYWORDS} keywords of up to "
                         f"{MAX_KEYWORD_LEN} characters")
    source = form.get("source", "auto")
    if source not in SOURCES:
        raise ValueError("Pick where to search from the list")
    grid = _number(form, "grid", 1, int, 1, 19, "Coverage")
    if grid not in GRIDS:
        raise ValueError("Pick a coverage from the list")
    return SearchParams(
        location=(form.get("location", "").strip() or config.DEFAULT_LOCATION)[:200],
        radius_miles=_number(form, "radius", config.DEFAULT_RADIUS_MILES, float, 1, 100,
                             "Radius"),
        keywords=keywords,
        source=source,
        min_score=_number(form, "min_score", config.DEFAULT_MIN_SCORE, int, 0, 100,
                          "Minimum score"),
        grid=grid,
        max_requests=_number(form, "max_requests", None, int, 1, MAX_REQUESTS_LIMIT,
                             "The limit on paid lookups"),
        only_keyword_matches=form.get("only_keyword_matches") == "on",
    )


def _save(job, result, params, warnings):
    try:
        job["progress"]("Saving leads")
        job["new_leads"], _ = saved.save_search(result.leads, params.keywords)
        job["saved"] = True
    except Exception as exc:
        log.exception("Saving the search's leads failed")
        why = db_message(exc) if isinstance(exc, store.Unavailable) else (
            "the database had a problem")
        warnings.append(f"These leads were not saved: {why}")


def _record(day, result, job, warnings):
    """The day's record is written before the job says it is done, so the page's
    search history is up to date when it reloads it."""
    try:
        daily.finish(day, {"leads": len(result.leads), "new": job.get("new_leads"),
                           "found_near": result.location_label,
                           "details": result.stats, "warnings": warnings})
    except Exception:
        log.exception("Recording today's search failed; retrying with the count only")
        # Try once more with just the count: without it the day would look unfinished
        # and free up again after daily.STALE_SECONDS.
        try:
            daily.finish(day, {"leads": len(result.leads)})
        except Exception:
            log.exception("Recording today's search failed again")


def _worker(job, params, day):
    try:
        # Closed places come back too, so a saved one that has since closed is updated
        # (and leaves the saved list); they are not shown.
        result = run(replace(params, include_closed=True), job["progress"])
        for problem in result.problems:
            log.warning("Search %s: a source failed: %s", day, problem)
        warnings = [plain_warning(w) for w in result.warnings]
        _save(job, result, params, warnings)
        if not params.include_closed:
            result.leads = [lead for lead in result.leads
                            if lead.business_status != "CLOSED_PERMANENTLY"]
        result.warnings = warnings
        _record(day, result, job, warnings)
        job.update(state="done", result=result, message="Done", pct=100, step=len(STEPS))
    except GeocodeError as exc:
        log.info("Search %s: location not found: %s", day, exc)
        _fail(job, day, f"{exc} {NOT_USED_UP}", str(exc))
    except PipelineError as exc:
        log.warning("Search %s failed: %s %s", day, exc, exc.detail)
        _fail(job, day, f"{exc} {NOT_USED_UP} Try again in an hour.", str(exc))
    except Exception:  # show unexpected failures instead of spinning forever
        log.exception("Search %s failed unexpectedly", day)
        _fail(job, day, f"Something went wrong during the search. {NOT_USED_UP} Try again "
                        "in an hour; if it keeps happening, tell whoever looks after the "
                        "site.", "Something went wrong during the search.")


def _fail(job, day, message, reason):
    _give_back(day, reason)
    job.update(state="error", message=message)


def _give_back(day, reason=None):
    """A search that failed outright does not use up the day (it stays in the
    history as failed, with the reason)."""
    try:
        daily.release(day, reason)
    except Exception:
        log.exception("Giving back today's search failed")


def _claim(params):
    """Claim today's search: (day, None), or (None, a response for the page)."""
    try:
        day, done = daily.claim({"location": params.location, "radius": params.radius_miles,
                                 "keywords": ", ".join(params.keywords),
                                 "source": params.source})
    except Exception as exc:
        log.error("Recording the search failed", exc_info=True)
        if isinstance(exc, store.Unavailable) and "DATABASE_URL" in str(exc):
            return None, (jsonify({"error": f"Searching needs the database: {exc}."}), 503)
        return None, (jsonify({"error": "Can't reach the saved data right now, so the search "
                                        "didn't start. Nothing is lost. Try again in a "
                                        "minute."}), 503)
    if day is None:
        return None, (jsonify({"error": f"Today's search was already run ({done['when']}). "
                                        "Find Leads works once a day; the next search can "
                                        "run tomorrow.", "searched": done}), 409)
    return day, None


@bp.post("/search")
def search():
    if not same_origin():
        abort(403)
    if config.switched_on(config.SEARCH_PAUSED_ENV):
        return jsonify({"error": SEARCH_PAUSED, "paused": True}), 503
    try:
        params = parse_form(request.form)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    s = state()
    running = s.running_job()
    if running is not None:
        # The page attaches to it (e.g. after a reload) instead of starting another.
        return jsonify({"error": "A search is already running.", "job_id": running}), 429
    day, refused = _claim(params)
    if refused:
        return refused
    job_id = uuid.uuid4().hex[:12]
    job = {"state": "running", "message": "Starting", "params": params,
           "skipped": skipped_steps(params)}
    job["progress"] = _Progress(job)
    with s.lock:
        s.jobs[job_id] = job
        # Evict the oldest finished jobs; never a running one.
        while len(s.jobs) > MAX_JOBS:
            old = next((k for k, v in s.jobs.items() if v["state"] != "running"), None)
            if old is None:
                break
            del s.jobs[old]
    try:
        threading.Thread(target=_worker, args=(job, params, day), daemon=True).start()
    except RuntimeError:
        log.exception("Starting the search thread failed")
        with s.lock:
            s.jobs.pop(job_id, None)
        _give_back(day)
        return jsonify({"error": "Could not start the search. Try again."}), 503
    return jsonify({"job_id": job_id})


@bp.get("/searches")
def searches():
    """Each day's search (when, what, what it found), and whether today's is used."""
    try:
        body = daily.history()
    except Exception as exc:
        log.error("Loading the search history failed", exc_info=True)
        return jsonify({"error": db_message(exc)}), 503
    for record in [body.get("current"), *body["searches"]]:
        if record and record.get("details"):
            record["details"] = plain_details(record["details"])
    current = body.get("current")
    if current:
        # When an unfinished search stops holding the day, in Utah time like every time.
        current["free_at"] = clock_text(current["at"] + daily.STALE_SECONDS)
    return jsonify({**body, "yelp": yelp_quota(), "running": state().running_job(),
                    "paused": SEARCH_PAUSED if switches()["search_paused"] else None})


@bp.get("/status/<job_id>")
def status(job_id):
    job = state().jobs.get(job_id) or abort(404)
    progress = job["progress"]
    body = {"state": job["state"], "message": job["message"], "steps": STEPS,
            "step": job.get("step", 0), "pct": round(progress.pct(), 1),
            "elapsed": round(time.time() - job.get("started", time.time())),
            "skipped": job.get("skipped", []),
            "slow": job["state"] == "running" and progress.slow()}
    if job["state"] == "done":
        res = job["result"]
        leads, undo = res.leads, {}
        warnings, shows_saved, saved_count = list(res.warnings), False, None
        if job.get("saved"):
            try:
                leads, undo = load_saved()
                saved_count, shows_saved = len(leads), True
            except LoadError as exc:
                warnings.append(str(exc))
        body.update(leads=[lead_json(lead, undo) for lead in leads],
                    stats=plain_details(res.stats), new_leads=job.get("new_leads"),
                    saved_count=saved_count, warnings=warnings, location=res.location_label,
                    yelp=yelp_quota(), saved=shows_saved)
    return jsonify(body)
