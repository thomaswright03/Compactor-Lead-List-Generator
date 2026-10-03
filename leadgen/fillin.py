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
# The note earlier versions kept in the day's record while the missing areas were
# filled in; the page leaves it out (coverage() says it once, see below).
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
            "areas": result.osm_areas, "boxes": osm.part_boxes(result.osm_missing),
            "found": 0, "new": 0, "rounds": 0,
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
            fill["boxes"] = osm.part_boxes(parts)      # which areas are still missing (the Map page)
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
        fill["boxes"] = osm.part_boxes(parts)
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
           done: bool = False) -> None:
    """The day's record with the fill-in's state, its counts added to the search's; once
    done and complete, the search is no longer incomplete."""
    try:
        record = daily.info(day) or {}
        changes: dict[str, Any] = {"fill": dict(fill)}
        for key, n in (more or {}).items():
            changes[key] = (record.get(key) or 0) + n
        if done and fill["state"] == COMPLETE:
            changes["partial"] = False
        daily.finish(day, changes)
    except Exception:
        log.exception("Recording how the filling in of the map areas stands failed")


def _finish(day: str, fill: dict[str, Any], unsaved: int) -> None:
    """Record how the filling in ended (the page words it, coverage()); parts that never
    answered are reported as a problem (the administrator's list and the webhook)."""
    if unsaved:
        log.warning("Search %s: %d businesses found while filling in couldn't be saved", day, unsaved)
    if fill["state"] not in (COMPLETE, STOPPED):
        left, areas = fill["left"], fill.get("areas") or 0
        if left:
            missing = (f"{left} of the {areas} parts of the area" if areas > 1 else "part of the area")
            final = (f"{missing[:1].upper()}{missing[1:]}{_around(fill)} never came in from the free map "
                     "data today, so businesses there are missing until the next search.")
        else:
            final = f"{unsaved:,} businesses found while filling in couldn't be saved."
        alerts.report("search", f"Today's search stayed incomplete: {final}")
    _write(day, fill, done=True)


def _around(fill: dict[str, Any]) -> str:
    """" (around Salt Lake City and Magna)": which towns the missing parts hold."""
    return f" (around {fill['where']})" if fill.get("where") else ""


# ---- how much of its area a search covered, in a salesperson's words
#
# A search whose free map data answered for only part of its area says so once on the
# Find leads page (coverage()["text"]) and once, briefly, in its history's Details
# (coverage()["short"]): how much of the area is covered, which towns are still being
# filled in and until when, that the businesses found can be called now, and that the
# rest appear on their own. The day's record keeps the numbers (its details' "osm areas
# searched", the filling in's "areas", "left", "where" and state); the words are worked
# out from them each time, so records saved by earlier versions read the same way and
# nothing rewrites a record. Internal counts (how many parts were asked again) are never
# shown.

# "about 2 of 9 areas" (the search's own figure), and the towns an incomplete search's
# reason names as missing.
_FIRST = re.compile(r"about (\d+) of (\d+) areas")
_NOT_YET = re.compile(r"; not yet: ([^)]*)\)")
# An incomplete search's reason when the free map data answered for part of the area.
_PARTLY = "answered for only part of the area"


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def coverage(record: dict[str, Any], paused: bool = False, earlier: bool = False) -> dict[str, Any] | None:
    """How much of its area the day's search covered, in words (see above): {"state",
    "text" (the Find page's one status), "short" (its Details line), "filling", "found"},
    or None when the free map data answered for the whole area. paused: searching is
    paused now (a filling in still going is stopping); earlier: the search is an earlier
    day's, still filling in (it stops when today's search starts)."""
    fill: dict[str, Any] = record["fill"] if isinstance(record.get("fill"), dict) else {}
    details: dict[str, Any] = record["details"] if isinstance(record.get("details"), dict) else {}
    searched = str(details.get("osm areas searched") or "")
    partly = bool(record.get("partial")) and _PARTLY in str(record.get("reason") or "")
    if not fill.get("areas") and not searched and not partly:
        return None
    state = str(fill.get("state") or "partial")
    first = _FIRST.match(searched)
    if fill.get("areas"):
        parts, left = int(fill["areas"]), int(fill.get("left") or 0)
        towns = str(fill.get("where") or "")
    else:
        parts = int(first.group(2)) if first else 1
        left = parts - int(first.group(1)) if first else 1
        said = _NOT_YET.search(str(record.get("reason") or ""))
        towns = said.group(1) if said else ""
    if not left:
        state = COMPLETE
    radius = record.get("radius")
    area = f"{radius:g}-mile area" if isinstance(radius, (int, float)) and radius else "area"
    covered = (f"about {parts - left} of the {parts} parts of the {area}" if parts > 1 else f"part of the {area}")
    short = f"About {parts - left} of {parts} parts" if parts > 1 else "Part of the area"
    found, new, leads = int(fill.get("found") or 0), int(fill.get("new") or 0), int(record.get("leads") or 0)
    them = (f"The {leads:,} businesses already found are" if leads != 1 else "The business already found is")
    ready = f"{them} on the Leads page, ready to call now"
    rest = towns or "the rest of the area"
    if state == COMPLETE:
        later = f" ({found:,} more businesses, {new:,} new)" if found else ""
        text = (f"Covered: the whole {area}. The last parts came in later, in the background{later}. "
                "Everything found is on the Leads page, ready to call.")
        short = (f"All {parts} parts" if parts > 1 else "The whole area") + \
            (f" ({found:,} businesses came in later, in the background)" if found else ", some later in the background")
    elif state == FILLING and paused:
        text = (f"Covered so far: {covered}. Stopping the filling in of {rest}, because searching was paused… "
                f"{ready}.")
        short = f"{short} so far; stopping the filling in (searching was paused)"
    elif state == FILLING:
        until = f" until about {fill['until_text']}" if fill.get("until_text") else ""
        text = (f"Covered so far: {covered}. Still filling in, in the background{until}: {rest}. {ready}; "
                "new ones from the rest of the area will appear there on their own.")
        short = f"{short} so far; filling in {rest}{until.replace(' about', '')}"
        if earlier:
            text = f"The search of {record.get('when') or record.get('day')} is still filling in. {text} " \
                   "It stops when today's search starts."
    elif state == STOPPED:
        because = NEW_SEARCH_TEXT if fill.get("why") == NEW_SEARCH else "searching was paused"
        text = (f"Covered: {covered}. Filling in stopped because {because}, so these weren't searched today: "
                f"{rest}. {ready}.")
        short = f"{short}; not searched: {rest} ({because})"
    elif state == INTERRUPTED:
        text = (f"Covered: {covered}. A server restart cut the filling in short, so businesses in {rest} are "
                f"missing until the next search. {ready}.")
        short = f"{short}; missing: {rest} (a server restart cut the filling in short)"
    else:                                    # gave up, or never filled in
        text = (f"Covered: {covered}. {_cap(rest)} didn't come in today, so businesses there are missing until "
                f"the next search. {ready}.")
        short = f"{short}; {rest} didn't come in"
    return {"state": state, "text": text, "short": short, "filling": state == FILLING, "found": found}


# What earlier versions stored in a search's notes about its coverage (coverage() says it
# once now): the whole note, or (beside another source's problem) the clause about it.
_RESTATED = re.compile(r"^(?:The map data service(?: \(OpenStreetMap\))? answered for only part of the area"
                       r"|Complete: the map areas"
                       r"|Filling in the missing map areas|The missing map areas answered later"
                       r"|The map areas that didn't answer)"
                       r"|of the free map data(?: \([^)]*\))? never answered today")
_CLAUSE = re.compile(r" and the map data service answered for only part of the area(?: \([^)]*\))?, so some "
                     r"of its businesses are missing")
# An earlier version's coverage words in a reason the history shows (a search that gave
# its day back): "the map data service answered for only part of the area (about 2 of 9
# areas searched; not yet: Layton)".
_OLD_WORDS = re.compile(r"(t|T)he map data service answered for only part of the area"
                        r"(?: \(about (\d+) of (\d+) areas searched(?:; [^:)]*: ([^)]*))?\))?")


def plain_notes(notes: list[Any]) -> list[str]:
    """A search's notes as the page shows them: without what they said about how much of
    the area the free map data covered (coverage() says that once)."""
    out = []
    for note in notes:
        if not isinstance(note, str):
            continue
        note = _CLAUSE.sub("", note)
        if note != NOTE and not _RESTATED.search(note):
            out.append(note)
    return out


def plain_reason(text: str) -> str:
    """A search's reason with an earlier version's coverage words in the page's words."""
    def words(m: re.Match[str]) -> str:
        said = f"{m.group(1)}he free map data covered only part of the area"
        if m.group(2):
            said = (f"{m.group(1)}he free map data covered only about {m.group(2)} of the {m.group(3)} parts of "
                    "the area")
        return said + (f" (missing: {m.group(4)})" if m.group(4) else "")
    return _OLD_WORDS.sub(words, text)
