"""Filling in, in the background, the map areas a day's search missed.

The free map servers sometimes refuse part of a wide search (busy or throttling).
The search itself asks those parts again a little (osm.search), then reports what
it found and uses up the day as usual. The parts still missing are then asked
again here, in the background, within the same search: a round every
config.FILL_IN_PAUSE_SECONDS for up to config.FILL_IN_SECONDS. The businesses they
find are merged, scored and saved like the search's own, so they join the saved
list as they arrive, and the day's record says how it stands ("fill": filling,
complete, gave_up, stopped). It is never a second search: the day stays used,
Yelp and Google are not asked, and nothing already saved is changed except by the
usual merge of a business found again.

A fill-in can run past midnight (Utah time). It then stays on the Find leads page
(daily.history's "filling") until it ends, and when the next day's search starts it
is ended first (end_others), so two map searches never ask the public servers at
once; its record then says it stopped for the new search.
"""

import logging
import re
import threading
import time
from dataclasses import replace
from typing import Any

from . import THREAD_PREFIX, alerts, config, daily, saved
from .models import Lead
from .pipeline import RunResult, SearchParams, finish_leads
from .sources import osm

log = logging.getLogger(__name__)

# The day's record's "fill" states. A "filling" one past its end (the server
# restarted mid-way) reads as INTERRUPTED (daily.history).
FILLING, COMPLETE, GAVE_UP, STOPPED = "filling", "complete", "gave_up", "stopped"
INTERRUPTED = "interrupted"
# The notes the search itself left about the missing areas (dropped once they are in).
PART_OF_AREA = "answered for only part of the area"
# The note the page shows while the missing areas are filled in (dropped when it ends).
NOTE = ("The map areas that didn't answer are being asked again in the background for up "
        "to an hour; their businesses join your saved leads as they arrive, and the search "
        "history below says when it's complete. This is still today's one search.")

# The background threads, by day (only one search a day, so one fill-in at most).
running: dict[str, threading.Thread] = {}
# Set to end a day's fill-in early (the next day's search is starting), by day.
_ending: dict[str, threading.Event] = {}
# Why a fill-in stopped (the record's fill "why"): searching was paused, or the next
# day's search started.
PAUSED, NEW_SEARCH = "paused", "new_search"
NEW_SEARCH_TEXT = "the next day's search started"
# How long the next day's search waits for an earlier fill-in to end (seconds).
END_WAIT_SECONDS = 60.0
# Off in the tests, except those about filling in (they switch it on).
ON = True


def wanted(result: RunResult) -> bool:
    """True when the search missed map areas that can be asked again."""
    return "osm" in result.partial_sources and bool(result.osm_missing)


def start(day: str, params: SearchParams, result: RunResult) -> bool:
    """Record that the missing areas are being filled in and start doing so; False
    when there is nothing to fill in or the thread can't start."""
    if not ON or not wanted(result):
        return False
    left = osm.areas_left(result.osm_missing, result.osm_areas)
    fill = {"state": FILLING, "left": left, "start_left": left,
            "where": where(result.osm_missing, result, params),
            "areas": result.osm_areas, "found": 0, "new": 0, "rounds": 0,
            "until": time.time() + config.FILL_IN_SECONDS}
    try:
        daily.finish(day, {"fill": fill})
        thread = threading.Thread(target=_run, args=(day, params, result, fill), daemon=True,
                                  name=f"{THREAD_PREFIX}fill-in {day}")
        _ending[day] = threading.Event()
        running[day] = thread
        thread.start()
    except Exception:
        log.exception("Starting to fill in the missing map areas failed")
        running.pop(day, None)
        _ending.pop(day, None)
        return False
    return True


def where(parts: list[osm.Part], result: RunResult, params: SearchParams) -> str:
    """The missing parts in words, nearest the centre first ("Salt Lake City and West
    Valley City"): the page says which towns are still being filled in."""
    try:
        return osm.areas_text(parts, result.center[0], result.center[1], params.radius_miles)
    except Exception:                 # only words for the page; never stops the fill-in
        log.warning("Naming the missing map areas failed", exc_info=True)
        return ""


def end_others(day: str, wait: float | None = None) -> list[str]:
    """End every fill-in of another day than `day` (a new day's search is starting)
    and wait for each to finish (up to `wait` seconds, END_WAIT_SECONDS by default),
    so the two never ask the map servers at once. Returns the days ended."""
    ended = []
    for other, thread in list(running.items()):
        if other == day:
            continue
        ending = _ending.get(other)
        if ending is not None:
            ending.set()
        thread.join(END_WAIT_SECONDS if wait is None else wait)
        if thread.is_alive():
            log.warning("The fill-in of %s didn't end in time; the new search starts anyway", other)
        ended.append(other)
    return ended


def _stop(day: str) -> str | None:
    """Why the day's fill-in must stop now: searching was paused, or the next day's
    search is starting."""
    if config.stop_reason("osm"):
        return PAUSED
    ending = _ending.get(day)
    return NEW_SEARCH if ending is not None and ending.is_set() else None


def _pause(day: str, ending: threading.Event) -> str | None:
    """Wait config.FILL_IN_PAUSE_SECONDS between rounds (the busy servers cool down),
    looking every osm.STOP_CHECK_SECONDS at whether the fill-in must stop, so pausing
    searching ends it within seconds, not at the next round; returns why it must
    stop (_stop), or None once the wait is over."""
    until = time.monotonic() + config.FILL_IN_PAUSE_SECONDS
    while True:
        why = _stop(day)
        left = until - time.monotonic()
        if why or left <= 0:
            return why
        ending.wait(min(left, osm.STOP_CHECK_SECONDS))


def _run(day: str, params: SearchParams, result: RunResult, fill: dict[str, Any]) -> None:
    parts = list(result.osm_missing)
    lat, lon = result.center
    keywords = [k.strip() for k in params.keywords if k and k.strip()]
    unsaved: list[Lead] = []                 # found, but the database didn't answer yet
    ending = _ending.setdefault(day, threading.Event())
    try:
        while parts and time.time() + config.FILL_IN_PAUSE_SECONDS < fill["until"]:
            why = _pause(day, ending)                    # let the busy servers cool down
            if why:
                fill["state"], fill["why"] = STOPPED, why
                break
            found, parts = osm.fill_in(lat, lon, params.radius_miles, keywords, parts,
                                       config.OVERPASS_RETRY_SECONDS, stop=lambda: _stop(day))
            fill["rounds"] += 1
            fill["left"] = osm.areas_left(parts, result.osm_areas)
            fill["where"] = where(parts, result, params)
            log.info("Search %s: filling in the map data, round %d: %d businesses, %d areas left",
                     day, fill["rounds"], len(found), fill["left"])
            unsaved += finish_leads(found, result.center, replace(params, include_closed=True),
                                    keywords)
            unsaved = _save(day, unsaved, fill)
            why = _stop(day)
            if why and parts:
                fill["state"], fill["why"] = STOPPED, why
                break
        if fill["state"] == FILLING:
            fill["state"] = COMPLETE if not parts and not unsaved else GAVE_UP
    except Exception:
        log.exception("Filling in the missing map areas failed")
        fill["state"] = GAVE_UP
    finally:
        fill["left"] = osm.areas_left(parts, result.osm_areas)
        fill["where"] = where(parts, result, params) if parts else ""
        _finish(day, fill, len(unsaved))
        running.pop(day, None)
        _ending.pop(day, None)


def _save(day: str, leads: list[Lead], fill: dict[str, Any]) -> list[Lead]:
    """Save what a round found (and what an earlier round couldn't) into the saved
    list and the day's record; returns the leads still unsaved."""
    if not leads:
        _write(day, fill)
        return []
    try:
        # finish_leads scored them with the standard words, like every saved lead.
        new, _ = saved.save_search(leads, config.DEFAULT_KEYWORDS)
    except Exception:
        log.exception("Saving the businesses of the filled-in map areas failed; trying again next round")
        _write(day, fill)
        return leads
    found = sum(1 for lead in leads if lead.business_status != "CLOSED_PERMANENTLY")
    fill["found"] += found
    fill["new"] += new
    _write(day, fill, {"leads": found, "new": new})
    return []


def _write(day: str, fill: dict[str, Any], more: dict[str, int] | None = None,
           final: str | None = None) -> None:
    """The day's record with the fill-in's state, its counts added to the search's."""
    try:
        record = daily.info(day) or {}
        changes: dict[str, Any] = {"fill": dict(fill)}
        for key, n in (more or {}).items():
            changes[key] = (record.get(key) or 0) + n
        details = dict(record.get("details") or {})
        details["osm areas filled in later"] = _areas_text(fill)
        changes["details"] = details
        if final is not None:
            warnings = [w for w in record.get("warnings") or [] if w != NOTE
                        and (fill["state"] != COMPLETE or PART_OF_AREA not in w)]
            changes["warnings"] = warnings + [final]
            if fill["state"] == COMPLETE:
                changes["partial"] = False
        daily.finish(day, changes)
    except Exception:
        log.exception("Recording how the filling in of the map areas stands failed")


def _areas_text(fill: dict[str, Any]) -> str:
    return (f"{fill['found']:,} businesses ({fill['new']:,} new)"
            + (f", {fill['left']} areas still missing{_around(fill)}" if fill["left"]
               else ", every area in"))


def _around(fill: dict[str, Any]) -> str:
    """" (around Salt Lake City and Magna)": which towns the missing areas hold."""
    return f" (around {fill['where']})" if fill.get("where") else ""


def _finish(day: str, fill: dict[str, Any], unsaved: int) -> None:
    left = fill["left"]
    added = (f" {fill['found']:,} more businesses were added to the saved list "
             f"({fill['new']:,} new)." if fill["found"] else "")
    lost = (f" {unsaved:,} businesses found then couldn't be saved (the saved data wasn't "
            "answering)." if unsaved else "")
    if fill["state"] == COMPLETE:
        final = f"Complete: the map areas that didn't answer at first were filled in later.{added}"
    elif fill["state"] == STOPPED:
        because = NEW_SEARCH_TEXT if fill.get("why") == NEW_SEARCH else "searching was paused"
        # Not the map servers' fault: the areas left were never asked again.
        final = (f"Filling in the missing map areas was stopped because {because}"
                 + (f", so {_areas(left)}{_around(fill)} {'was' if left == 1 else 'were'} not asked again."
                    if left else ".")
                 + added + lost)
    else:
        missing = (f"{_areas(left)[:1].upper()}{_areas(left)[1:]} of the free map data"
                   f"{_around(fill)} never answered today, so their businesses are missing "
                   "until the next search." if left else "The missing map areas answered later.")
        final = missing + added + lost
        alerts.report("search", f"Today's search stayed incomplete: {final}")
    _write(day, fill, final=final)


def _areas(n: int) -> str:
    return "1 area" if n == 1 else f"{n} areas"


# ---- the day's record as the page shows it, once its missing areas were asked again
#
# The search writes how much of the map data answered ("about 2 of 9 areas searched; not
# yet: Salt Lake City, ...") before the background asks the missing areas again. The
# record keeps those words; settled() rewrites them for the page from how the filling in
# stands (its "left", "where" and state), so every line of the history and its Details
# names the same missing towns, and areas left because searching was paused (or the next
# day's search started) read as not asked, never as "never answered".

_COVERAGE = re.compile(r"about (\d+) of (\d+) areas searched(?:; not yet: [^)]*)?")
_FIRST = re.compile(r"about (\d+) of (\d+) areas$")
_ASKED_AGAIN = re.compile(r"^(\d+) \(some (?:never answered|didn't answer)\)$")
# Stored by earlier versions when the administrator's pause stopped a fill-in.
_STOPPED_NEVER = re.compile(r"(stopped because (?:searching was paused|the next day's search started)); "
                            r"(\d+ areas?)( \(around [^)]*\))? never answered\.")
# Why areas are still missing, by the fill-in's state (and, when stopped, why it stopped).
_LEFT_WHY = {FILLING: "still being asked", COMPLETE: "", GAVE_UP: "never answered",
             INTERRUPTED: "still missing (a server restart cut the filling in short)",
             PAUSED: "not asked: searching was paused", NEW_SEARCH: f"not asked: {NEW_SEARCH_TEXT}"}


def _why_left(fill: dict[str, Any]) -> str:
    state = fill.get("state", "")
    if state == STOPPED:
        return _LEFT_WHY[NEW_SEARCH if fill.get("why") == NEW_SEARCH else PAUSED]
    return _LEFT_WHY.get(state, "still missing")


def settled(record: dict[str, Any]) -> dict[str, Any]:
    """The day's record with its coverage words brought up to date with its filling in
    (see above); a copy: the stored record is unchanged. A record without one is
    returned as it is."""
    fill = record.get("fill")
    if not isinstance(fill, dict) or not fill.get("areas"):
        return record
    areas, left = int(fill["areas"]), int(fill.get("left") or 0)
    why = _why_left(fill)
    record = dict(record)
    details = dict(record.get("details") or {})
    first = _FIRST.match(str(details.get("osm areas searched", "")))
    before = int(first.group(1)) if first else None
    # Areas answered since the search: fewer missing than at its start ("start_left"; an
    # older record without it counts from the search's own figure, once it was asked again).
    if isinstance(fill.get("start_left"), int):
        later = max(0, fill["start_left"] - left)
    else:
        later = max(0, areas - left - before) if before is not None and fill.get("rounds") else 0
    answered = areas if not left else min(areas - 1, before + later) if before is not None else areas - left
    if before is not None and areas > 1:
        if not left:
            details["osm areas searched"] = f"all {areas} areas (about {before} during the search, the rest later)"
        elif later:
            details["osm areas searched"] = (f"about {answered} of {areas} areas (about {before} during the "
                                             f"search, {answered - before} later)")
    asked = _ASKED_AGAIN.match(str(details.get("osm areas asked again", "")))
    if asked and fill.get("state") != GAVE_UP:
        details["osm areas asked again"] = (f"{asked.group(1)} (some answered only later, in the background)"
                                            if not left else f"{asked.group(1)} (some areas {why})")
    if "osm areas filled in later" in details:
        so_far = " so far" if fill.get("state") == FILLING else ""
        details["osm areas filled in later"] = (
            f"{fill.get('found', 0):,} businesses ({fill.get('new', 0):,} new){so_far}"
            + (f", {_areas(left)} {why}{_around(fill)}" if left else ", every area in"))
    record["details"] = details
    where = fill.get("where") or ""
    if not left:
        coverage = f"all {areas} areas searched in the end, some only later in the background"
    else:
        # "not asked: searching was paused" reads "not asked (searching was paused): Kaysville".
        said = re.sub(r"^(not asked): (.*)$", r"\1 (\2)", why) if fill.get("state") != FILLING else "not yet"
        coverage = f"about {answered} of {areas} areas searched" + (f"; {said}: {where}" if where else "")

    def fixed(text: str) -> str:
        text = _COVERAGE.sub(lambda m: coverage, text)
        return _STOPPED_NEVER.sub(lambda m: f"{m.group(1)}, so {m.group(2)}{m.group(3) or ''} "
                                            f"{'was' if m.group(2) == '1 area' else 'were'} not asked again.", text)

    if isinstance(record.get("reason"), str):
        record["reason"] = fixed(record["reason"])
    record["warnings"] = [fixed(w) if isinstance(w, str) else w for w in record.get("warnings") or []]
    return record
