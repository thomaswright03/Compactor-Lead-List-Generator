"""The Find leads page's server side: start today's search, follow it, its history."""

import logging
import math
import re
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from flask import Blueprint, Response, abort, jsonify, request
from flask.typing import ResponseReturnValue

from .. import alerts, config, daily, saved, store
from .. import switches as site_switches
from ..geo import GeocodeError
from ..localtime import clock_text, date_time_text
from ..pipeline import GRIDS, SOURCES, PipelineError, RunResult, SearchParams, run
from .auth import admin_open, admin_refusal, admin_state
from .common import (
    NOT_USED_UP,
    SEARCH_PAUSED,
    db_message,
    same_origin,
    state,
    switches,
    yelp_quota,
)

log = logging.getLogger("leadgen.web")

# A running or finished search, as the page follows it (state, step, pct, result...).
Job = dict[str, Any]
Num = int | float

MAX_JOBS = 20
MAX_KEYWORDS = 20
MAX_KEYWORD_LEN = 60
MAX_REQUESTS_LIMIT = 5000

STEPS = ["Find the location", "Search Yelp and Google", "Search map data", "Merge and score",
         "Save"]
# A step running longer than this (seconds) is "taking longer than usual" on the page.
SLOW_SECONDS = [30, 240, 150, 60, 60]

# The search's numbers, as the page names them (the search history's "Details"), in the
# order they read as a funnel: found per source, within the radius, merged, left out,
# kept, then the kept leads by tier and what the search cost. "min score" goes into the
# low-score line's label.
DETAIL_LABELS = {
    "google raw results": "Businesses from Google",
    "yelp raw results": "Businesses from Yelp",
    "osm raw results": "Businesses from the free map data",
    "osm areas searched": "Free map data: areas that answered",
    "osm areas asked again": "Free map data: areas asked again automatically",
    "results in radius": "Listings within the radius",
    "duplicates merged": "Duplicate listings merged",
    "after dedupe": "Businesses after merging duplicates",
    "closed": "Left out: closed for good",
    "below min score": "Left out: score below the minimum",
    "not matching keywords": "Left out: not matching the search words",
    "over limit": "Left out: over the lead limit",
    "leads kept": "Leads kept",
    "competitors flagged": "Competitors among them (flagged)",
    "tier A": "Tier A leads",
    "tier B": "Tier B leads",
    "tier C": "Tier C leads",
    "tier D": "Tier D leads",
    "google requests": "Google lookups",
    "yelp requests": "Yelp calls",
    "seconds": "Took",
}
# Left-out lines that are only shown when something was left out that way.
_ONLY_IF_ANY = ("closed", "not matching keywords", "over limit", "duplicates merged")


class FormError(ValueError):
    """A form value the server refuses; `field` names the input the page shows it next to."""

    def __init__(self, message: str, field: str = "") -> None:
        super().__init__(message)
        self.field = field


bp = Blueprint("finding", __name__)

_TECHNICAL = re.compile(r"https?://|HTTP \d{3}|Error\b|Exception|<html|\(\w+Error", re.I)
_SOURCE_WORDS = (("Google", "Google"), ("Yelp", "Yelp"), ("OpenStreetMap", "The map data service"),
                 ("Overpass", "The map data service"))


def plain_warning(text: str) -> str:
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


def plain_details(details: dict[str, object] | None) -> list[list[object]]:
    """A search's numbers as [label, value] pairs in funnel order (older records keep
    their names). A list, because JSON objects lose their order on the way to the page."""
    details = dict(details or {})
    minimum = details.pop("min score", None)
    order = list(DETAIL_LABELS)
    out = []
    for key in sorted(details, key=lambda k: order.index(k) if k in order else len(order)):
        value = details[key]
        if key in _ONLY_IF_ANY and not value:
            continue
        label = DETAIL_LABELS.get(key, key)
        if key == "below min score" and minimum is not None:
            label = f"Left out: score below {minimum}"
        if key == "seconds" and isinstance(value, (int, float)):
            value = f"{round(value)} seconds" if value < 120 else f"{round(value / 60)} minutes"
        out.append([label, value])
    return out


_AREAS = re.compile(r"(\d+) of (\d+) areas done")


def plain_progress(msg: str) -> str:
    """The search's progress message in the page's words."""
    page = re.match(r"(Yelp|Google) page \d+: '(.*)' \((\d+)/(\d+)\)", msg)
    if page:
        return f"Searching {page.group(1)} for {page.group(2)} ({page.group(3)} of {page.group(4)})"
    if msg.startswith("Locating"):
        return "Finding the location"
    if msg.startswith(("Yelp: ", "Google: ")):
        return f"Searching {msg.split(':')[0]}"
    if msg.startswith("OpenStreetMap"):
        areas = _AREAS.search(msg)
        if areas:
            again = (" (some areas are being asked again in smaller parts)"
                     if "again" in msg else "")
            return (f"Searching the free map data: {int(areas.group(1)):,} of "
                    f"{int(areas.group(2)):,} areas done{again}…")
        # Which mirror is answering means nothing to a salesperson; only say one is being tried.
        server = re.search(r"server (\d+) of \d+", msg)
        return ("Searching the free map data"
                + (", trying another source" if server and server.group(1) != "1" else "") + "…")
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

    def __init__(self, job: Job) -> None:
        self.job = job
        self.calls = 0
        self.cap: int | None = None
        self.map_started: float | None = None
        self.areas_done = self.areas_total = 0
        self.step_started = time.time()
        job.update(step=0, pct=1.0, started=time.time())

    def slow(self) -> bool:
        step = self.job["step"]
        return step < len(SLOW_SECONDS) and time.time() - self.step_started > SLOW_SECONDS[step]

    def __call__(self, msg: str) -> None:
        job = self.job
        areas = _AREAS.search(msg)
        if areas:
            # Parts finish on several threads, so a message can arrive after a newer
            # one: the page's count only moves forward.
            self.areas_done = max(self.areas_done, int(areas.group(1)))
            self.areas_total = max(self.areas_total, int(areas.group(2)))
            msg = _AREAS.sub(f"{self.areas_done} of {self.areas_total} areas done", msg)
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
            # A wide search is asked in parts: the bar moves on with each one done.
            self._at(2, 50 + (36 * self.areas_done / self.areas_total if areas else 0))
        elif msg.startswith("Filtering"):
            self._at(3, 88)
        elif msg.startswith("Merging"):
            self._at(3, 92)
        elif msg.startswith("Saving"):
            self._at(4, 96)

    def _at(self, step: int, pct: float) -> None:
        if step > self.job["step"]:
            self.step_started = time.time()
        self.job["step"] = max(self.job["step"], step)
        self.job["pct"] = max(self.job["pct"], pct)

    def pct(self) -> float:
        if self.job["step"] == 2 and self.map_started:
            # Creeps toward 86% over the few minutes the map servers take.
            return max(float(self.job["pct"]),
                       50 + 36 * (1 - math.exp(-(time.time() - self.map_started) / 150)))
        return float(self.job["pct"])


def skipped_steps(params: SearchParams) -> list[int]:
    """Steps a search won't run: Yelp and Google when neither is set up (or both are
    switched off), map data when a paid source alone was picked."""
    google, yelp_key, _ = params.resolved_keys()
    paid = (params.source in ("google", "both", "yelp")
            or (params.source == "auto" and (google or yelp_key)))
    return ([] if paid else [1]) + ([] if params.source in ("osm", "both", "auto") else [2])


def _number(form: Mapping[str, str], name: str, default: Num | None, cast: type, lo: float, hi: float,
            label: str, unit: str = "") -> Any:
    """A number field; its errors use the field's on-screen label (and unit)."""
    raw = (form.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise FormError(f"{label} must be a number", name) from None
    if cast is int:
        if not math.isfinite(value) or not value.is_integer():
            raise FormError(f"{label} must be a whole number", name)
        value = int(value)
    if not lo <= value <= hi:
        raise FormError(f"{label} must be between {lo:g} and {hi:g}{unit}", name)
    return value


def parse_form(form: Mapping[str, str]) -> SearchParams:
    """Validate the form strictly; raise FormError with a message for the page (and the
    field it is about)."""
    # The page always sends the field; a bare POST without it (a script) searches around Arco.
    location = (form.get("location", config.DEFAULT_LOCATION) or "").strip()
    if not location:
        raise FormError("Enter where to search around: a ZIP code, city or street address.",
                        "location")
    if len(location) > 200:
        raise FormError("Search around is too long: use at most 200 characters.", "location")
    keywords = [k.strip() for k in form.get("keywords", "").split(",") if k.strip()]
    if len(keywords) > MAX_KEYWORDS:
        raise FormError(f"Use at most {MAX_KEYWORDS} search words (you have {len(keywords)}).",
                        "keywords")
    long = next((k for k in keywords if len(k) > MAX_KEYWORD_LEN), None)
    if long is not None:
        raise FormError(f"Each search word can be up to {MAX_KEYWORD_LEN} characters; "
                        f"“{long[:20]}…” has {len(long)}. Separate words with commas.",
                        "keywords")
    source = form.get("source", "auto")
    if source not in SOURCES:
        raise FormError("Pick where to search from the list", "source")
    # The page always sends How far and the minimum score: a blank one is a mistake to
    # point out, never a silent default (a script that leaves them out gets the defaults).
    for name, message in (("radius", "How far must be between 1 and 100 miles"),
                          ("min_score", "The score to leave out weak leads below must be "
                                        "between 0 and 100")):
        if name in form and not (form.get(name) or "").strip():
            raise FormError(message, name)
    grid = _number(form, "grid", 1, int, 1, 19, "Coverage")
    if grid not in GRIDS:
        raise FormError("Pick a coverage from the list", "grid")
    return SearchParams(
        location=location,
        radius_miles=_number(form, "radius", config.DEFAULT_RADIUS_MILES, float, 1, 100,
                             "How far", " miles"),
        # The standard words are always searched; the ones typed are searched as well.
        keywords=list(dict.fromkeys(config.DEFAULT_KEYWORDS
                                    + [k for k in keywords if k.lower() not in config.DEFAULT_KEYWORDS])),
        source=source,
        min_score=_number(form, "min_score", config.DEFAULT_MIN_SCORE, int, 0, 100,
                          "The score to leave out weak leads below"),
        grid=grid,
        max_requests=_number(form, "max_requests", None, int, 1, MAX_REQUESTS_LIMIT,
                             "Most paid lookups for this search"),
        only_keyword_matches=form.get("only_keyword_matches") == "on",
    )


def _save(job: Job, result: RunResult, params: SearchParams) -> str | None:
    """Save the search's leads; None when saved, else why not (in the page's words)."""
    try:
        job["progress"]("Saving leads")
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
        daily.finish(day, {"leads": len(result.leads), "new": job.get("new_leads"),
                           "found_near": result.location_label,
                           "details": result.stats, "warnings": warnings, **(extra or {})})
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
    try:
        # Closed places come back too, so a saved one that has since closed is updated
        # (it stays in the saved list, flagged closed for good); they are not shown here.
        result = run(replace(params, include_closed=True), job["progress"])
        for problem in result.problems:
            log.warning("Search %s: a source failed: %s", day, problem)
        warnings = [plain_warning(w) for w in result.warnings]
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
        if result.failed_sources:
            _incomplete(day, result, job, warnings)
        else:
            _record(day, result, job, warnings)
        job.update(state="done", result=result, message="Done", pct=100, step=len(STEPS))
    except GeocodeError as exc:
        log.info("Search %s: location not found: %s", day, exc)
        _fail(job, day, f"{exc} {NOT_USED_UP}", str(exc), alert=False)
    except PipelineError as exc:
        log.warning("Search %s failed: %s %s", day, exc, exc.detail)
        _fail(job, day, f"{exc} {NOT_USED_UP} {RETRY}", str(exc))
    except Exception:  # show unexpected failures instead of spinning forever
        log.exception("Search %s failed unexpectedly", day)
        _fail(job, day, f"Something went wrong during the search. {NOT_USED_UP} {RETRY} If it "
                        "keeps happening, tell whoever looks after the site.",
                        "Something went wrong during the search.")


def _incomplete(day: str, result: RunResult, job: Job, warnings: list[str]) -> None:
    """A search where a source failed (a paid one refused its key, say) while others
    found businesses: what was found is saved, and the day is given back, so the
    search can be run again once the source works; but only daily.INCOMPLETE_RERUNS
    more times (0: none, the owner's rule): an incomplete search after that keeps
    the day, like a complete one."""
    names = _source_names(result.failed_sources)
    paid = [s for s in result.failed_sources if s in ("google", "yelp")]
    whole = [s for s in result.failed_sources if s not in result.partial_sources]
    parts = [f"Couldn't reach {_source_names(whole)}, so its businesses are missing"] if whole else []
    coverage = result.stats.get("osm areas searched")
    if result.partial_sources:
        parts.append(f"{_source_names(result.partial_sources)} answered for only part of the "
                     f"area{f' ({coverage} searched)' if coverage else ''}, so some of its "
                     "businesses are missing")
    reason = (" and ".join(parts)[:1].upper() + " and ".join(parts)[1:]
              + f" from this search; the {len(result.leads):,} businesses found were "
              + ("saved." if job.get("saved") else "kept."))
    advice = (f" If it keeps happening, ask whoever looks after the site to check the "
              f"{_source_names(paid)} key." if paid else "")
    how = ("answered for only part of the area" if not whole
           else "couldn't be reached" if not result.partial_sources else "didn't fully answer")
    missing = (f"Some businesses are missing: {names} {how}"
               + (f" ({coverage} searched)." if coverage and not whole else "."))
    try:
        earlier = daily.incomplete_count(day)
    except Exception:
        log.exception("Counting today's incomplete searches failed")
        earlier = 0
    if earlier >= daily.INCOMPLETE_RERUNS:
        # The re-run allowance is used up: this search keeps the day, like a complete one.
        used = ("This was today's re-run, so today's search is now used up"
                if earlier else "Today's search is used up all the same (one search a day)")
        warnings.append(f"{reason} {used}; the next search can run tomorrow, from midnight "
                        f"Utah time.{advice}")
        job["note"] = f"{missing} Today's search is now used up; the next one can run tomorrow."
        _record(day, result, job, warnings, {"partial": True, "reason": reason})
    else:
        left = daily.INCOMPLETE_RERUNS - earlier
        job["note"] = f"{missing} Run the search again today to fill them in."
        warnings.append(f"{NOT_USED_UP} You can run it again {_times(left)} today to fill in "
                        f"what {names} would have found.{advice}")
        _give_back(day, reason, {"partial": True, "leads": len(result.leads),
                                 "new": job.get("new_leads"), "details": result.stats,
                                 "warnings": warnings})
    alerts.report("search", f"Today's search was incomplete: {reason}{advice}")


def _times(n: int) -> str:
    return "once" if n == 1 else f"{n} times"


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


def _claim(params: SearchParams) -> str | tuple[Response, int]:
    """Claim today's search: the day, or the response for the page when it can't be had."""
    try:
        day, done = daily.claim({"location": params.location, "radius": params.radius_miles,
                                 "keywords": ", ".join(params.keywords),
                                 "source": params.source})
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
    claimed = _claim(params)
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
        threading.Thread(target=_worker, args=(job, params, day), daemon=True).start()
    except RuntimeError:
        log.exception("Starting the search thread failed")
        with s.lock:
            s.jobs.pop(job_id, None)
        _give_back(day)
        return jsonify({"error": "Could not start the search. Try again."}), 503
    return jsonify({"job_id": job_id})


@bp.get("/searches")
def searches() -> ResponseReturnValue:
    """Each day's search (when, what, what it found), and whether today's is used."""
    try:
        body = daily.history()
    except Exception as exc:
        log.error("Loading the search history failed", exc_info=True)
        return jsonify({"error": db_message(exc)}), 503
    for record in [body.get("current"), *body["searches"]]:
        if record and record.get("details"):
            record["details"] = plain_details(record["details"])
    problems: dict[str, Any] | None = None           # only for an unlocked administrator
    if admin_open():
        try:
            problems = alerts.recent()
        except Exception:
            log.warning("Loading the recent problems failed", exc_info=True)   # the history is still worth showing
    body["problems"] = problems
    current = body.get("current")
    if current:
        # When an unfinished search stops holding the day, in Utah time like every time.
        current["free_at"] = clock_text(current["at"] + daily.STALE_SECONDS)
    return jsonify({**body, "yelp": yelp_quota(), "running": state().running_job(),
                    "paused": SEARCH_PAUSED if switches()["search_paused"] else None,
                    "switches": _switch_list(), "admin": admin_state()})


def _switch_list() -> dict[str, Any]:
    """The emergency switches for the page, each with when it was flipped in Utah time."""
    out = site_switches.state()
    for item in out.values():
        item["when"] = date_time_text(item.pop("at")) if item["site"] else ""
    return out


@bp.post("/switches")
def flip_switch() -> ResponseReturnValue:
    """Flip an emergency switch inside the site: it takes effect on the next request (a
    running search stops at its next check), with no restart."""
    if not same_origin():
        abort(403)
    if not admin_open():
        return admin_refusal()
    data = request.get_json(silent=True) or {}
    name = site_switches.KEYS.get(str(data.get("key") or ""))
    if name is None or not isinstance(data.get("on"), bool):
        return jsonify({"error": "Unknown switch."}), 400
    try:
        site_switches.set_switch(name, data["on"], str(data.get("by") or ""))
    except Exception as exc:
        log.error("Saving a switch failed", exc_info=True)
        return jsonify({"error": db_message(exc)}), 503
    return jsonify({"switches": _switch_list()})


@bp.get("/status/<job_id>")
def status(job_id: str) -> ResponseReturnValue:
    job = state().jobs.get(job_id) or abort(404)
    progress = job["progress"]
    body = {"state": job["state"], "message": job["message"], "steps": STEPS,
            "step": job.get("step", 0), "pct": round(progress.pct(), 1),
            "elapsed": round(time.time() - job.get("started", time.time())),
            "skipped": job.get("skipped", []),
            "slow": job["state"] == "running" and progress.slow()}
    if job["state"] == "done":
        # Only what the page shows: the counts, not the leads (the Leads page loads those).
        res = job["result"]
        body.update(found=len(res.leads), new_leads=job.get("new_leads"),
                    saved_count=job.get("saved_count"), warnings=list(res.warnings),
                    note=job.get("note"),
                    location=res.location_label, yelp=yelp_quota(), saved=bool(job.get("saved")))
    return jsonify(body)
