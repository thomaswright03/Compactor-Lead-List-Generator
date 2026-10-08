"""The Find leads page's server side: start today's search (POST /search), run it in
the background and follow it (GET /status), and record how it went.

The rest of the page lives beside it, one job each: search_form.py (the form, checked),
place.py (where the search will run, GET /place), search_progress.py (the progress bar),
search_words.py (notes, numbers and progress in the page's words), history.py (the search
history, GET /searches) and switch_routes.py (the emergency switches, POST /switches).
Their names stay importable from here as well.
"""

import logging
import re
import threading
import time
import uuid
from dataclasses import replace
from typing import Any

from flask import Blueprint, Response, abort, jsonify, request
from flask.typing import ResponseReturnValue

from .. import THREAD_PREFIX, alerts, config, daily, fillin, interrupted, saved, store
from ..geo import GeocodeError, LookupDown
from ..localtime import date_time_text
from ..pipeline import PipelineError, RunResult, SearchParams, SearchStopped, run
from ..progress import SAVE, Step
from ..sources import collecting, osm, paging
from .common import NOT_USED_UP, SEARCH_PAUSED, db_message, same_origin, state, yelp_quota
from .history import _for_page, _recover, searches
from .place import CONFIRM_MILES, _place, confirmed, doubt_message, far_message, place, where
from .search_form import MAX_KEYWORD_LEN, MAX_KEYWORDS, MAX_REQUESTS_LIMIT, FormError, Num, parse_form
from .search_progress import (
    CREEP_PART,
    CREEP_SECONDS,
    MAP_STEP,
    PAID_STEP,
    SLOW_SECONDS,
    STEP_WEIGHTS,
    STEPS,
    Job,
    _Progress,
    skipped_steps,
)
from .search_words import DETAIL_LABELS, plain_details, plain_progress, plain_warning
from .switch_routes import _switch_list, flip_switch

# Every name this module had before the split (2026-10-08), so `from .finding import x` and
# `finding.x` keep working.
__all__ = ["CONFIRM_MILES", "CREEP_PART", "CREEP_SECONDS", "DETAIL_LABELS", "MAP_STEP", "MAX_JOBS",
           "MAX_KEYWORDS", "MAX_KEYWORD_LEN", "MAX_REQUESTS_LIMIT", "PAID_STEP", "RETRY", "SLOW_SECONDS",
           "STEPS", "STEP_WEIGHTS", "FormError", "Job", "Num", "_Progress", "_for_page", "_place", "_recover",
           "_switch_list", "bp", "confirmed", "far_message", "flip_switch", "map_areas", "parse_form", "place",
           "plain_details", "plain_progress", "plain_warning", "search", "searches", "skipped_steps", "status",
           "where"]

log = logging.getLogger("leadgen.web")

MAX_JOBS = 20

bp = Blueprint("finding", __name__)


def _save(job: Job, result: RunResult, params: SearchParams) -> str | None:
    """Save the search's leads; None when saved, else why not (in the page's words)."""
    try:
        job["progress"](Step("Saving leads", SAVE, 1, 3))
        job["new_leads"], _ = saved.save_search(result.leads, params.keywords)
        job["saved"] = True
    except Exception as exc:
        log.exception("Saving the search's leads failed")
        if isinstance(exc, store.Unavailable) and "DATABASE_URL" in str(exc):
            return db_message(exc)
        return "the saved-leads database couldn't be reached."
    try:
        job["saved_count"] = saved.count()
    except Exception:
        # The leads are saved; only the page's "N saved leads in all" goes without its number.
        log.exception("Counting the saved leads failed")
    return None


def _record(day: str, result: RunResult, job: Job, warnings: list[str],
            extra: dict[str, Any] | None = None) -> None:
    """The day's record is written before the job says it is done, so the page's
    search history is up to date when it reloads it."""
    try:
        areas = {"map_areas": job["map_areas"]} if job.get("map_areas") else {}
        daily.finish(day, {"leads": len(result.leads), "new": job.get("new_leads"),
                           "found_near": result.location_label,
                           "details": result.stats, "warnings": warnings, **areas, **(extra or {})})
    except Exception:
        log.exception("Recording today's search failed; retrying with the count only")
        # Try once more with just the count: without it the day would look unfinished
        # and free up again after daily.STALE_SECONDS.
        try:
            daily.finish(day, {"leads": len(result.leads)})
        except Exception:
            log.exception("Recording today's search failed again")


# After a failed search: one piece of advice, the same wherever the page shows it.
RETRY = "You can try again now; if it fails again, try later today."


def _worker(job: Job, params: SearchParams, day: str) -> None:
    # What the sources return is written to the database as the search goes, so a
    # server restart mid-search keeps it and gives the day back (interrupted.py).
    checkpoint = interrupted.Run(day, params)
    checkpoint.start()
    try:
        _search(job, params, day, checkpoint)
    finally:
        checkpoint.end()


def _search(job: Job, params: SearchParams, day: str, checkpoint: interrupted.Run) -> None:
    try:
        # An earlier day's fill-in still asking the map servers ends first (fillin.py):
        # two map searches at once make the busy public servers refuse more of both.
        fillin.end_others(day)
        # Closed places come back too, so a saved one that has since closed is updated
        # (it stays in the saved list, flagged closed for good); they are not shown here.
        with collecting(checkpoint.add):
            result = run(replace(params, include_closed=True), job["progress"])
        for problem in result.problems:
            log.warning("Search %s: a source failed: %s", day, problem)
        warnings = _when_reused([plain_warning(w) for w in result.warnings], params, day)
        unsaved = _save(job, result, params)
        if unsaved is not None:
            # Leads that aren't saved are lost with the page: the search gives the day
            # back (like a failed one), so it can be run again once the database answers.
            found = f"The search found {len(result.leads):,} businesses, but they couldn't be saved"
            _fail(job, day, f"{found}: {unsaved} {NOT_USED_UP} {RETRY}", f"{found}: {unsaved}")
            return
        if not params.include_closed:
            result.leads = [lead for lead in result.leads
                            if lead.business_status != "CLOSED_PERMANENTLY"]
        result.warnings = warnings
        job["map_areas"] = map_areas(result, params)
        if result.stopped:
            _stopped(day, result, job, warnings)
        elif result.failed_sources:
            _incomplete(day, result, job, warnings, params)
        else:
            _record(day, result, job, warnings)
        job.update(state="done", result=result, message="Done", pct=100, step=len(STEPS))
    except LookupDown as exc:
        log.warning("Search %s: the place couldn't be looked up: %s", day, exc)
        _fail(job, day, str(exc), "The place couldn't be looked up (the lookup services weren't answering).")
    except GeocodeError as exc:
        log.info("Search %s: location not found: %s", day, exc)
        _fail(job, day, f"{exc} {NOT_USED_UP}", str(exc), alert=False)
    except SearchStopped as exc:
        # The administrator's deliberate stop: recorded as stopped, not as a failure (no alert).
        log.info("Search %s was stopped by the administrator: %s", day, exc.reason)
        _give_back(day, str(exc), {"stopped": exc.reason})
        job.update(state="error", stopped=exc.reason, message=f"{exc} {NOT_USED_UP}")
    except PipelineError as exc:
        log.warning("Search %s failed: %s %s", day, exc, exc.detail)
        _fail(job, day, f"{exc} {NOT_USED_UP} {RETRY}", str(exc))
    except Exception:  # show unexpected failures instead of spinning forever
        log.exception("Search %s failed unexpectedly", day)
        _fail(job, day, f"Something went wrong during the search. {NOT_USED_UP} {RETRY} If it "
                        "keeps happening, tell whoever looks after the site.",
                        "Something went wrong during the search.")


def map_areas(result: RunResult, params: SearchParams) -> dict[str, Any] | None:
    """What the day's record keeps about the free map data's areas (the Map page shades
    them): the boxes of the parts no server answered for ("missing", [] when every area
    answered; [None] when none did), or "known": False when the administrator stopped
    the search partway. None when the search didn't ask the map data."""
    if params.source not in ("auto", "osm", "both"):
        return None
    if result.stopped:
        return {"known": False}
    if "osm" in result.failed_sources and "osm" not in result.partial_sources:
        return {"missing": [None]}
    return {"missing": osm.part_boxes(result.osm_missing)}


def _when_reused(warnings: list[str], params: SearchParams, day: str) -> list[str]:
    """A paid source that reused saved answers (sources/paging.py REUSED) says when this
    area was searched before, so "0 new" reads as "nothing new since then"."""
    if not any(paging.REUSED in w for w in warnings):
        return warnings
    try:
        before = daily.earlier_search(params.place or params.location, day,
                                      time.time() - config.CACHE_TTL_SECONDS)
    except Exception:
        log.warning("Looking up the earlier search of the area failed", exc_info=True)
        before = None
    if before is None:
        return warnings
    first = next(i for i, w in enumerate(warnings) if paging.REUSED in w)
    when = date_time_text(before["at"])
    return [*warnings[:first], f"This area was also searched on {when}, and the answers saved then were reused "
            "where they could be: what was found then counts as updated, not new.", *warnings[first:]]


def _stopped(day: str, result: RunResult, job: Job, warnings: list[str]) -> None:
    """A search the administrator stopped (searching was paused while it ran): what it
    had found is saved, the rest of the area wasn't searched, and the day stays used
    (one search a day). Its record and the page say who stopped it."""
    kept = f"the {len(result.leads):,} businesses it had found were saved" if job.get("saved") \
        else f"the {len(result.leads):,} businesses it had found were kept"
    job["stopped"] = result.stopped
    job["note"] = (f"Stopped by the administrator ({result.stopped}): {kept}; the rest of the "
                   "area wasn't searched. Today's search is used up.")
    _record(day, result, job, warnings, {"stopped": result.stopped})


def _incomplete(day: str, result: RunResult, job: Job, warnings: list[str],
                params: SearchParams) -> None:
    """A search where a source failed (a paid one refused its key, say) while others
    found businesses: what was found is saved and the day is used up, like a complete
    search (one search a Utah day, the owner's rule: no same-day re-run). Map areas the
    free map servers missed are then filled in in the background (fillin.py), within
    this same search.

    How much of the area the free map data covered is said once, by the Find page's
    status (fillin.coverage, from the record's numbers): the notes and the summary
    beside the result only name a source that couldn't be reached at all."""
    paid = [s for s in result.failed_sources if s in ("google", "yelp")]
    whole = [s for s in result.failed_sources if s not in result.partial_sources]
    n = len(result.leads)
    found = (f"the {n:,} business{'es' if n != 1 else ''} found {'were' if n != 1 else 'was'} "
             + ("saved." if job.get("saved") else "kept."))
    advice = (f" If it keeps happening, ask whoever looks after the site to check the "
              f"{_source_names(paid)} key." if paid else "")
    # The record's reason, for the administrator's problem list and the webhook (the
    # coverage words name the towns still missing, nearest the centre first).
    parts = [f"Couldn't reach {_source_names(whole)}, so its businesses are missing"] if whole else []
    coverage = result.stats.get("osm areas searched")
    if result.partial_sources:
        around = fillin.where(result.osm_missing, result, params) if coverage and result.osm_missing else ""
        said = f"{coverage} searched; not yet: {around}" if around else f"{coverage} searched" if coverage else ""
        parts.append(f"{_source_names(result.partial_sources)} answered for only part of the "
                     f"area{f' ({said})' if said else ''}, so some of its businesses are missing")
    reason = _cap(" and ".join(parts)) + f" from this search; {found}"
    job["note"] = None
    if whole:
        # One note for a source that couldn't be reached (the search's own plain one is
        # replaced by this, which also says what was saved and that the day is used up).
        warnings[:] = [w for w in warnings if not _UNREACHED.match(w)]
        missing = f"Couldn't reach {_source_names(whole)}, so its businesses are missing from this search"
        warnings.append(f"{missing}; {found} Today's search is used up all the same (one search a day); the next "
                        f"search can run tomorrow, from midnight Utah time.{advice}")
        job["note"] = f"{missing}. Today's search is now used up; the next one can run tomorrow."
    _record(day, result, job, warnings, {"partial": True, "reason": reason})
    filling = fillin.ON and fillin.wanted(result) and fillin.start(day, params, result)
    if filling and not whole:
        # Only map areas are missing, and they are being filled in: fillin.py reports
        # a problem if some never answer.
        return
    alerts.report("search", f"Today's search was incomplete: {reason}{advice}")


# The search's own note about a source it couldn't reach (pipeline._check_found).
_UNREACHED = re.compile(r"^Couldn't reach .+ this time, so its businesses are missing from this search\.$")


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def _source_names(sources: list[str]) -> str:
    names = [{"google": "Google", "yelp": "Yelp", "osm": "the map data service"}[s]
             for s in sources]
    return " and ".join(names)


def _fail(job: Job, day: str, message: str, reason: str, alert: bool = True) -> None:
    _give_back(day, reason)
    if alert:
        alerts.report("search", f"Today's search failed: {reason}")
    job.update(state="error", message=message)


def _give_back(day: str, reason: str | None = None, extra: dict[str, Any] | None = None) -> None:
    """A search that failed (outright, or a source of it) does not use up the day
    (it stays in the history as failed, with the reason)."""
    try:
        daily.release(day, reason, extra)
    except Exception:
        log.exception("Giving back today's search failed")


def _claim(params: SearchParams, place: dict[str, Any]) -> str | tuple[Response, int]:
    """Claim today's search: the day, or the response for the page when it can't be had."""
    try:
        day, done = daily.claim({"location": params.location, "radius": params.radius_miles,
                                 "keywords": ", ".join(params.keywords),
                                 "source": params.source, "place": place["label"],
                                 "miles": place["miles"], "center": [place["lat"], place["lon"]]})
    except Exception as exc:
        log.error("Recording the search failed", exc_info=True)
        if isinstance(exc, store.Unavailable) and "DATABASE_URL" in str(exc):
            return jsonify({"error": f"Searching needs the database: {exc}."}), 503
        return (jsonify({"error": "Can't reach the saved data right now, so the search "
                                        "didn't start. Nothing is lost. Try again in a "
                                        "minute."}), 503)
    if day is None:
        when = done["when"] if done else "earlier today"
        return jsonify({"error": f"Today's search was already run ({when}). Find Leads works "
                                 "once a day; the next search can run tomorrow, from midnight "
                                 "Utah time.",
                        "searched": done}), 409
    return day


@bp.post("/search")
def search() -> ResponseReturnValue:
    if not same_origin():
        abort(403)
    if config.switched_on(config.SEARCH_PAUSED_ENV):
        return jsonify({"error": SEARCH_PAUSED, "paused": True}), 503
    try:
        params = parse_form(request.form)
    except FormError as exc:
        return jsonify({"error": str(exc), "field": exc.field}), 400
    s = state()
    running = s.running_job()
    if running is not None:
        # The page attaches to it (e.g. after a reload) instead of starting another.
        return jsonify({"error": "A search is already running.", "job_id": running}), 429
    # Where it will run is found first: a place that can't be found, or one outside
    # AARCO's area or only a road, trail or business (place.where "uncertain") that the
    # person hasn't confirmed, never uses up the day.
    found = _place(params)
    if isinstance(found, tuple):
        return found
    yes = confirmed(request.form.get("confirm_place"), found)
    if found["outside"] and not yes:
        return jsonify({"error": f"{far_message(found)} Check the place, or confirm that "
                                 "you want to search there.",
                        "field": "location", "place": found, "confirm_far": True}), 409
    if found["uncertain"] and not yes:
        return jsonify({"error": doubt_message(found), "field": "location", "place": found,
                        "confirm_uncertain": True}), 409
    params = replace(params, center=(found["lat"], found["lon"]), place=found["label"])
    _recover()                  # a search a restart cut off today gives the day back first
    claimed = _claim(params, found)
    if not isinstance(claimed, str):
        return claimed
    day = claimed
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
        threading.Thread(target=_worker, args=(job, params, day), daemon=True,
                         name=f"{THREAD_PREFIX}search {day}").start()
    except RuntimeError:
        log.exception("Starting the search thread failed")
        with s.lock:
            s.jobs.pop(job_id, None)
        _give_back(day)
        return jsonify({"error": "Could not start the search. Try again."}), 503
    return jsonify({"job_id": job_id})


@bp.get("/status/<job_id>")
def status(job_id: str) -> ResponseReturnValue:
    job = state().jobs.get(job_id) or abort(404)
    progress = job["progress"]
    body = {"state": job["state"], "message": job["message"], "steps": STEPS,
            "stopping": job["state"] == "running" and job["message"].startswith("Stopping"),
            "step": job.get("step", 0), "pct": round(progress.pct(), 1),
            "elapsed": round(time.time() - job.get("started", time.time())),
            "skipped": job.get("skipped", []),
            "slow": job["state"] == "running" and progress.slow()}
    if job["state"] == "error" and job.get("stopped"):
        body["stopped"] = job["stopped"]           # stopped by the administrator, not a failure
    if job["state"] == "done":
        # Only what the page shows: the counts, not the leads (the Leads page loads those).
        res = job["result"]
        body.update(found=len(res.leads), new_leads=job.get("new_leads"),
                    saved_count=job.get("saved_count"), warnings=fillin.plain_notes(list(res.warnings)),
                    note=job.get("note"), stopped=job.get("stopped"),
                    location=res.location_label, yelp=yelp_quota(), saved=bool(job.get("saved")))
    return jsonify(body)
