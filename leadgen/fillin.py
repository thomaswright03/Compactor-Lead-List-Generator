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
"""

import logging
import threading
import time
from dataclasses import replace
from typing import Any

from . import alerts, config, daily, saved
from .models import Lead
from .pipeline import RunResult, SearchParams, finish_leads
from .sources import osm

log = logging.getLogger(__name__)

# The day's record's "fill" states. A "filling" one past its end (the server
# restarted mid-way) reads as INTERRUPTED (daily.history).
FILLING, COMPLETE, GAVE_UP, STOPPED = "filling", "complete", "gave_up", "stopped"
# The notes the search itself left about the missing areas (dropped once they are in).
PART_OF_AREA = "answered for only part of the area"
# The note the page shows while the missing areas are filled in (dropped when it ends).
NOTE = ("The map areas that didn't answer are being asked again in the background for up "
        "to an hour; their businesses join your saved leads as they arrive, and the search "
        "history below says when it's complete. This is still today's one search.")

# The background threads, by day (only one search a day, so one fill-in at most).
running: dict[str, threading.Thread] = {}
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
    fill = {"state": FILLING, "left": osm.areas_left(result.osm_missing, result.osm_areas),
            "areas": result.osm_areas, "found": 0, "new": 0, "rounds": 0,
            "until": time.time() + config.FILL_IN_SECONDS}
    try:
        daily.finish(day, {"fill": fill})
        thread = threading.Thread(target=_run, args=(day, params, result, fill), daemon=True,
                                  name=f"fill-in {day}")
        running[day] = thread
        thread.start()
    except Exception:
        log.exception("Starting to fill in the missing map areas failed")
        running.pop(day, None)
        return False
    return True


def _run(day: str, params: SearchParams, result: RunResult, fill: dict[str, Any]) -> None:
    parts = list(result.osm_missing)
    lat, lon = result.center
    keywords = [k.strip() for k in params.keywords if k and k.strip()]
    unsaved: list[Lead] = []                 # found, but the database didn't answer yet
    try:
        while parts and time.time() + config.FILL_IN_PAUSE_SECONDS < fill["until"]:
            time.sleep(config.FILL_IN_PAUSE_SECONDS)   # let the busy servers cool down
            if config.stop_reason("osm"):
                fill["state"] = STOPPED
                break
            found, parts = osm.fill_in(lat, lon, params.radius_miles, keywords, parts,
                                       config.OVERPASS_RETRY_SECONDS)
            fill["rounds"] += 1
            fill["left"] = osm.areas_left(parts, result.osm_areas)
            log.info("Search %s: filling in the map data, round %d: %d businesses, %d areas left",
                     day, fill["rounds"], len(found), fill["left"])
            unsaved += finish_leads(found, result.center, replace(params, include_closed=True),
                                    keywords)
            unsaved = _save(day, unsaved, fill)
        if fill["state"] == FILLING:
            fill["state"] = COMPLETE if not parts and not unsaved else GAVE_UP
    except Exception:
        log.exception("Filling in the missing map areas failed")
        fill["state"] = GAVE_UP
    finally:
        fill["left"] = osm.areas_left(parts, result.osm_areas)
        _finish(day, fill, len(unsaved))
        running.pop(day, None)


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
            + (f", {fill['left']} areas still missing" if fill["left"] else ", every area in"))


def _finish(day: str, fill: dict[str, Any], unsaved: int) -> None:
    left = fill["left"]
    added = (f" {fill['found']:,} more businesses were added to the saved list "
             f"({fill['new']:,} new)." if fill["found"] else "")
    lost = (f" {unsaved:,} businesses found then couldn't be saved (the saved data wasn't "
            "answering)." if unsaved else "")
    if fill["state"] == COMPLETE:
        final = f"Complete: the map areas that didn't answer at first were filled in later.{added}"
    elif fill["state"] == STOPPED:
        final = ("Filling in the missing map areas was stopped because searching was paused"
                 + (f"; {_areas(left)} never answered." if left else ".") + added + lost)
    else:
        missing = (f"{_areas(left)[:1].upper()}{_areas(left)[1:]} of the free map data never "
                   "answered today, so their businesses are missing until the next search."
                   if left else "The missing map areas answered later.")
        final = missing + added + lost
        alerts.report("search", f"Today's search stayed incomplete: {final}")
    _write(day, fill, final=final)


def _areas(n: int) -> str:
    return "1 area" if n == 1 else f"{n} areas"
