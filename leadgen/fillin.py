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
from typing import Any, NamedTuple

from . import THREAD_PREFIX, alerts, area_map, config, daily, saved, store
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

# The background threads, by day (only one search a day, so one fill-in at most); in
# sign-up mode by company and day ("<company id> <day>", _key).
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


def _key(day: str) -> str:
    """The day's fill-in's key in running and _ending: the day, or in sign-up mode the
    company (store.company_id) and the day, so two companies' fill-ins never mix."""
    company = store.company_id()
    return day if company is None else f"{company} {day}"


def _company_of(key: str) -> str | None:
    return key.split(" ")[0] if " " in key else None


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
        thread = threading.Thread(target=store.carry(_run), args=(day, params, result, fill), daemon=True,
                                  name=f"{THREAD_PREFIX}fill-in {day}")
        _ending[_key(day)] = threading.Event()
        running[_key(day)] = thread
        thread.start()
    except Exception:
        log.exception("Starting to fill in the missing map areas failed")
        running.pop(_key(day), None)
        _ending.pop(_key(day), None)
        return False
    return True


def _tally(parts: list[osm.Part], result: RunResult, params: SearchParams) -> area_map.Tally:
    """How the search covers its areas with `parts` still missing, as the Map page counts them."""
    return area_map.tally(result.center[0], result.center[1], params.radius_miles, osm.part_boxes(parts))


def where(parts: list[osm.Part], result: RunResult, params: SearchParams) -> str:
    """The areas the missing parts leave not searched in full, in words, nearest the
    center first ("Salt Lake City and Layton"), named as the Map page names them: the
    page says which towns are still being filled in."""
    try:
        return area_map.names_text(_tally(parts, result, params).left) if parts else ""
    except Exception:                 # only words for the page; never stops the fill-in
        log.warning("Naming the missing map areas failed", exc_info=True)
        return ""


def end_others(day: str, wait: float | None = None) -> list[str]:
    """End every fill-in of another day than `day` (a new day's search is starting)
    and wait for each to finish (up to `wait` seconds, END_WAIT_SECONDS by default),
    so the two never ask the map servers at once. Returns the days ended."""
    ended = []
    for key, thread in list(running.items()):
        # Only this company's own (sign-up mode): another company's search is its own.
        if key == _key(day) or _company_of(key) != store.company_id():
            continue
        other = key.rsplit(" ", 1)[-1]
        ending = _ending.get(key)
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
    ending = _ending.get(_key(day))
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
    ending = _ending.setdefault(_key(day), threading.Event())
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
        try:
            counted: area_map.Tally | None = _tally(parts, result, params)
        except Exception:
            log.warning("Counting the missing map areas failed", exc_info=True)
            counted = None
        _finish(day, fill, len(unsaved), counted)
        running.pop(_key(day), None)
        _ending.pop(_key(day), None)


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


def _finish(day: str, fill: dict[str, Any], unsaved: int, counted: area_map.Tally | None = None) -> None:
    """Record how the filling in ended (the page words it, coverage()); parts that never
    answered are reported as a problem (the administrator's list and the webhook), with
    the areas counted as the pages count them (`counted`)."""
    if unsaved:
        log.warning("Search %s: %d businesses found while filling in couldn't be saved", day, unsaved)
    if fill["state"] not in (COMPLETE, STOPPED):
        left, areas = fill["left"], fill.get("areas") or 0
        if left and counted is not None and counted.left:
            left, areas = len(counted.left), counted.areas
        if left:
            missing = (f"{left} of the {areas} areas" if areas > 1 else "part of the area")
            # Areas some of which came in are not called missing as a whole (coverage()).
            how = "came in only in part, or not at all," if counted is not None and counted.partly else \
                "never came in"
            final = (f"{missing[:1].upper()}{missing[1:]}{_around(fill)} {how} from the free map "
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
# rest appear on their own. Each area not searched in full is named as the Map page
# shades it: partly searched (some of its businesses came in), or, once the filling in
# has ended, not searched; never one as missing when part of it came in. The day's
# record keeps the numbers (its details' "osm areas searched", the filling in's "areas",
# "left", "boxes", "where" and state); the words are worked out from them each time, so
# records saved by earlier versions read the same way and nothing rewrites a record.
# Internal counts (how many parts were asked again) are never shown.

# "about 2 of 9 areas" (the search's own figure), and the towns an incomplete search's
# reason names as missing.
_FIRST = re.compile(r"about (\d+) of (\d+) (?:areas|parts)")
_NOT_YET = re.compile(r"; not yet: ([^)]*)\)")
# An incomplete search's reason when the free map data answered for part of the area.
_PARTLY = "answered for only part of the area"


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


class _Extent(NamedTuple):
    """How much of its area a search covered, counted and named for coverage()'s words."""

    areas: int                 # the search's areas
    left: int                  # of them, not searched in full
    towns: str                 # the towns of the areas left, named
    some: str                  # the partly searched areas, named ("" when not known apart)
    none: str                  # the areas not searched at all, named
    covered: str               # "6 of the 9 areas within 30 miles searched in full"
    short: str                 # the same for the Details line: "6 of 9 areas searched in full"


def _counted_extent(counted: area_map.Tally, state: str, within: str) -> _Extent:
    """The extent as counted area by area (area_map.Tally): each area not searched in
    full named as the Map page names its status, partly searched (some of its
    businesses came in) or not searched at all."""
    areas, left = counted.areas, len(counted.left)
    if not left and state == FILLING:
        left = 1                             # only slivers are left: still filling in
    return _Extent(areas, left, area_map.names_text(counted.left), area_map.names_text(counted.partly),
                   area_map.names_text(counted.missed),
                   f"{areas - left} of the {areas} areas{within} searched in full",
                   f"{areas - left} of {areas} areas searched in full")


def _recorded_extent(record: dict[str, Any], fill: dict[str, Any], searched: str, within: str,
                     area: str) -> _Extent:
    """The extent from the record's own numbers (a record an earlier version saved, or
    one with no parts kept): the filling in's figures, else the search's "about 2 of 9
    areas" and the towns its reason names as missing."""
    first = _FIRST.match(searched)
    if fill.get("areas"):
        areas, left = int(fill["areas"]), int(fill.get("left") or 0)
        towns = str(fill.get("where") or "")
    else:
        areas = int(first.group(2)) if first else 1
        left = areas - int(first.group(1)) if first else 1
        said = _NOT_YET.search(str(record.get("reason") or ""))
        towns = said.group(1) if said else ""
    # A record an earlier version saved names only the areas missing, which the Map
    # page reads as not searched (area_map._named_missing).
    towns = area_map.plain_names(towns, record)
    return _Extent(areas, left, towns, "", towns,
                   f"about {areas - left} of the {areas} areas{within}" if areas > 1 else f"part of the {area}",
                   f"About {areas - left} of {areas} areas" if areas > 1 else "Part of the area")


def _extent(record: dict[str, Any], fill: dict[str, Any], searched: str, state: str, within: str,
            area: str) -> _Extent:
    """The search's extent: counted and named area by area, as the Map page does, from
    the parts the record keeps as missing, else from the record's numbers."""
    try:
        counted = area_map.record_tally(record)
    except Exception:
        log.warning("Counting the search's map areas failed", exc_info=True)
        counted = None
    if counted is not None and counted.areas > 1:
        return _counted_extent(counted, state, within)
    return _recorded_extent(record, fill, searched, within, area)


def _complete_text(ext: _Extent, fill: dict[str, Any], area: str) -> tuple[str, str]:
    """The status and Details line once the whole area has come in."""
    found, new = int(fill.get("found") or 0), int(fill.get("new") or 0)
    later = f" ({found:,} more businesses, {new:,} new)" if found else ""
    text = (f"Covered: the whole {area}. The last areas came in later, in the background{later}. "
            "Everything found is on the Leads page, ready to call.")
    short = (f"All {ext.areas} areas" if ext.areas > 1 else "The whole area") + \
        (f" ({found:,} businesses came in later, in the background)" if found else ", some later in the background")
    return text, short


def _filling_text(so_far: str, short_so_far: str, rest: str, ready: str, record: dict[str, Any],
                  fill: dict[str, Any], paused: bool, earlier: bool) -> tuple[str, str]:
    """The status and Details line while the missing areas are still being filled in
    (or that is stopping, searching having been paused)."""
    if paused:
        text = (f"Covered so far: {so_far}. Stopping the filling in of {rest}, because searching was paused… "
                f"{ready}.")
        return text, f"{short_so_far}; stopping the filling in (searching was paused)"
    until = f" until about {fill['until_text']}" if fill.get("until_text") else ""
    text = (f"Covered so far: {so_far}. Still filling in, in the background{until}: {rest}. {ready}; "
            "new ones from the rest of the area will appear there on their own.")
    short = f"{short_so_far}; filling in {rest}{until.replace(' about', '')}"
    if earlier:
        text = f"The search of {record.get('when') or record.get('day')} is still filling in. {text} " \
               "It stops when today's search starts."
    return text, short


def _coverage_text(state: str, ext: _Extent, record: dict[str, Any], fill: dict[str, Any], area: str,
                   paused: bool, earlier: bool) -> tuple[str, str]:
    """The Find page's status and its Details line for a search's state and extent."""
    leads = int(record.get("leads") or 0)
    them = (f"The {leads:,} businesses already found are" if leads != 1 else "The business already found is")
    ready = f"{them} on the Leads page, ready to call now"
    rest = ext.towns or "the rest of the area"
    # "6 of the 9 areas searched in full; partly searched: Salt Lake City", and (once the
    # filling in has ended) "; not searched: Layton and Park City".
    in_part = f"; partly searched: {ext.some}" if ext.some else ""
    not_searched = f"; not searched: {ext.none}" if ext.none else ""
    so_far, short_so_far = ext.covered + in_part, f"{ext.short} so far{in_part}"
    named, short_named = so_far + not_searched, ext.short + in_part + not_searched
    if state == COMPLETE:
        return _complete_text(ext, fill, area)
    if state == FILLING:
        return _filling_text(so_far, short_so_far, rest, ready, record, fill, paused, earlier)
    if state == STOPPED:
        because = NEW_SEARCH_TEXT if fill.get("why") == NEW_SEARCH else "searching was paused"
        text = (f"Covered: {named}. Filling in stopped because {because}, so the rest wasn't searched today. "
                f"{ready}.")
        return text, f"{short_named} ({because})"
    if state == INTERRUPTED:
        text = (f"Covered: {named}. A server restart cut the filling in short, so businesses in the rest of the "
                f"area are missing until the next search. {ready}.")
        return text, f"{short_named} (a server restart cut the filling in short)"
    # Gave up, or never filled in.
    text = (f"Covered: {named}. The rest didn't come in from the free map data today, so businesses there "
            f"are missing until the next search. {ready}.")
    return text, f"{short_named} (the rest didn't come in)"


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
    radius = record.get("radius")
    area = f"{radius:g}-mile area" if isinstance(radius, (int, float)) and radius else "area"
    within = f" within {radius:g} miles" if isinstance(radius, (int, float)) and radius else ""
    ext = _extent(record, fill, searched, state, within, area)
    if not ext.left:
        state = COMPLETE
    text, short = _coverage_text(state, ext, record, fill, area, paused, earlier)
    return {"state": state, "text": text, "short": short, "filling": state == FILLING,
            "found": int(fill.get("found") or 0)}


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
                        r"(?: \(about (\d+) of (\d+) (?:areas|parts) searched(?:; [^:)]*: ([^)]*))?\))?")


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
            said = f"{m.group(1)}he free map data covered only about {m.group(2)} of the {m.group(3)} areas"
        return said + (f" (missing: {m.group(4)})" if m.group(4) else "")
    return _OLD_WORDS.sub(words, text)
