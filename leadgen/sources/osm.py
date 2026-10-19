"""Free fallback source: OpenStreetMap via the Overpass API (no key needed).

Coverage is thinner than Google (fewer phone numbers and websites) but it
often includes warehouses and industrial buildings that Google lists poorly,
and building outlines give a rough size signal.

This module asks the map servers and runs a search's rounds; the query is built in
osm_query.py, the answers are read in osm_parse.py and the areas a search is cut into
are in osm_areas.py. Every name they had here is still imported from here.
"""

import logging
import queue
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Any

from .. import THREAD_PREFIX, config
from ..http import HttpError, cache_get, cache_put, request_json
from ..models import Lead
from ..progress import MAP, Step
from . import SourceError, report_found
from .osm_areas import (
    CENTER_AREA,
    MILES_PER_DEGREE_LAT,
    Box,
    Part,
    _distance,
    _parts,
    _quarters,
    area_boxes,
    area_name,
    areas_left,
    part_boxes,
)
from .osm_parse import CATEGORY_KEYS, MAX_BUILDING_SQFT, SQFT_PER_M2, is_part_label, parse_element
from .osm_query import build_query

# Re-exported: callers import these from osm, wherever they are written.
__all__ = ["CATEGORY_KEYS", "CENTER_AREA", "MAX_BUILDING_SQFT", "MILES_PER_DEGREE_LAT", "SQFT_PER_M2", "Box",
           "Part", "SourceError", "area_boxes", "area_name", "areas_left", "build_query", "is_part_label",
           "parse_element", "part_boxes"]

log = logging.getLogger(__name__)

# A mirror is only asked when at least this many seconds of the map-data time are left.
MIN_SECONDS_LEFT = 5
# While the map data is being asked, the administrator's pause (config.stop_reason) is
# looked at this often (seconds): a paused search stops within a few seconds.
STOP_CHECK_SECONDS = 2.0


class PartialResult(SourceError):
    """The map data came back for only part of the area: `leads` and `warnings` hold
    what the parts that answered found (the search keeps them, marked incomplete);
    `missing` the parts no server answered for, which fill_in can ask again later,
    and `areas` how many areas the search was cut into."""

    def __init__(self, message: str, leads: list[Lead], warnings: list[str],
                 coverage: str = "", missing: list[Part] | None = None, areas: int = 1) -> None:
        super().__init__(message)
        self.leads, self.warnings = leads, warnings
        self.coverage = coverage          # "about 7 of 9 areas": how much of the radius answered
        self.missing = list(missing or [])
        self.areas = areas


class Stopped(SourceError):
    """The administrator paused searching while the map data was being asked: `leads`
    and `warnings` hold what the parts that had answered found (the search keeps
    them); `reason` says who stopped it, in plain words."""

    def __init__(self, reason: str, leads: list[Lead], warnings: list[str]) -> None:
        super().__init__(f"Stopped: {reason}")
        self.reason, self.leads, self.warnings = reason, leads, warnings


def _answer_key(query: str) -> str:
    return "overpass-answer\n" + query


def _fetch(query: str, deadline: float, first: int = 0,
           said: Callable[[int, int], None] | None = None,
           cancel: threading.Event | None = None) -> tuple[Any, list[str]]:
    """Ask the mirrors (starting with mirror `first`) for one query's answer before
    `deadline` (time.monotonic()); returns (the answer, errors of mirrors that failed
    first) or raises SourceError when none answered, or as soon as `cancel` is set
    (the search was stopped: no other mirror is asked).

    A mirror that fails is followed at once by the next one; a mirror that is slow to
    answer is not waited out: after config.OVERPASS_STAGGER_SECONDS the next one is
    asked as well (the slow one may still answer), and the first good answer wins.
    """
    # An answer from any mirror today serves a re-run, whichever mirror it asks first
    # (and the day's filling in); tomorrow's search asks again (OVERPASS_CACHE_TTL_SECONDS).
    known = cache_get(_answer_key(query), config.OVERPASS_CACHE_TTL_SECONDS)
    if known is not None:
        return known, []
    endpoints = config.overpass_endpoints()
    endpoints = endpoints[first % len(endpoints):] + endpoints[:first % len(endpoints)]
    mirrors = _Mirrors(query, endpoints, said)
    data = mirrors.answer(deadline, cancel)
    if not data.get("remark"):
        cache_put(_answer_key(query), data)
    return data, mirrors.errors


class _Mirrors:
    """One query put to the Overpass mirrors in turn (_fetch): which have been asked,
    how many are still to answer, and what the ones that failed said."""

    def __init__(self, query: str, endpoints: list[str], said: Callable[[int, int], None] | None) -> None:
        self.query, self.endpoints, self.said = query, endpoints, said
        self.answers: queue.Queue[tuple[str, Any, BaseException | None]] = queue.Queue()
        self.errors: list[str] = []
        self.asked = self.waiting = 0
        self.next_at = 0.0

    def _ask(self, endpoint: str, left: float) -> None:
        """Ask one mirror (in its own thread) and queue its answer or error."""
        try:
            # Connecting gets at most 10 s; the answer may take the rest of the time.
            data = request_json("POST", endpoint, data={"data": self.query},
                                timeout=(min(10.0, left), left), retries=1,
                                cache_key_extra="overpass", cache_ttl=config.OVERPASS_CACHE_TTL_SECONDS,
                                cacheable=lambda d: not d.get("remark"))
        except HttpError as exc:
            self.answers.put((endpoint, None, exc))
        except Exception as exc:  # noqa: BLE001 - reported like a failed server
            self.answers.put((endpoint, None, exc))
        else:
            self.answers.put((endpoint, data, None))

    def due(self, now: float) -> bool:
        """True when it is the next mirror's turn: none asked yet, or the last one asked
        config.OVERPASS_STAGGER_SECONDS ago and still to answer."""
        return self.asked < len(self.endpoints) and (self.waiting == 0 or now >= self.next_at)

    def ask_next(self, now: float, left: float) -> bool:
        """Ask the next mirror, with `left` seconds to answer; False when there is no
        time for it and none asked is still to answer (nothing more to wait for)."""
        endpoint = self.endpoints[self.asked]
        if left < MIN_SECONDS_LEFT:
            self.errors.append("out of time before trying " + endpoint)
            self.asked = len(self.endpoints)        # no time for more; wait for the ones asked
            return self.waiting > 0
        self.asked += 1
        self.waiting += 1
        self.next_at = now + config.OVERPASS_STAGGER_SECONDS
        if self.said:
            self.said(self.asked, len(self.endpoints))
        log.info("OpenStreetMap: querying %s", endpoint.split("/")[2])
        threading.Thread(target=self._ask, args=(endpoint, left), daemon=True,
                         name=f"{THREAD_PREFIX}map server {endpoint.split('/')[2]}").start()
        return True

    def take(self, timeout: float) -> Any | None:
        """The next answer within `timeout` seconds: its data when good, else None (no
        answer yet, a failed mirror, or a mirror that only sent a remark)."""
        try:
            endpoint, data, exc = self.answers.get(timeout=max(0.01, timeout))
        except queue.Empty:
            return None
        self.waiting -= 1
        if exc is not None:
            log.warning("OpenStreetMap server failed: %s", exc)
            self.errors.append(str(exc))
            return None
        if data.get("remark") and not data.get("elements"):
            log.warning("OpenStreetMap server %s: %s", endpoint, data["remark"])
            self.errors.append(f"{endpoint}: {data['remark']}")
            return None
        return data

    def answer(self, deadline: float, cancel: threading.Event | None) -> Any:
        """The first good answer before `deadline`, asking the mirrors in turn; raises
        SourceError when none answered, or as soon as `cancel` is set."""
        while True:
            if cancel is not None and cancel.is_set():
                raise SourceError("stopped by the administrator")
            now = time.monotonic()
            left = deadline - now
            if self.due(now):
                if self.ask_next(now, left):
                    continue
                break
            if self.waiting == 0 or left <= 0:
                if self.waiting:
                    self.errors.append("no server answered in time")
                break
            until = left if self.asked >= len(self.endpoints) else min(left, self.next_at - now)
            if cancel is not None:
                until = min(until, 1.0)                 # look at the stop now and then
            data = self.take(until)
            if data is not None:
                return data
        raise SourceError(" | ".join(self.errors) or "no server answered")


class _Parts:
    """The state of one map-data search across its rounds: what was found, which
    parts are still missing, and the progress counts the page shows.

    The parts are asked nearest the centre first (the area the search centre is in,
    then outwards; a failed part's quarters take their place in that order), so the
    first businesses found, and the ones a search cut short keeps, are the nearest.
    Progress counts the search's areas (len(boxes), fixed for the whole search):
    an area answered in quarters counts once all four have answered."""

    def __init__(self, lat: float, lon: float, radius_miles: float, keywords: Sequence[str],
                 progress: Callable[[str], None] | None,
                 stop: Callable[[], str | None] | None = None) -> None:
        self.lat, self.lon, self.radius, self.keywords = lat, lon, radius_miles, keywords
        self.progress = progress
        # Why the search must stop now (config.stop_reason), looked at every
        # STOP_CHECK_SECONDS; once it says so, nothing more is asked.
        self.stop = stop
        self.stopped: str | None = None
        self.cancel = threading.Event()
        self.checked = 0.0
        self.boxes = _parts(lat, lon, radius_miles)
        self.whole = len(self.boxes) == 1
        self.found: dict[str, Lead] = {}
        self.warnings: list[str] = []
        self.errors: list[str] = []
        self.missing: list[Part] = []            # parts no server answered for (yet)
        self.done = 0                            # parts that answered
        self.share = 0.0                         # the share of the whole area they cover
        self.started = 0                         # queries sent (spreads parts over mirrors)
        self.split = False                       # a part of this round was asked in quarters
        self.again = ""                          # progress wording during a catch-up round

    def areas_done(self) -> int:
        """How many of the search's areas have answered (an area asked in quarters
        counts once all of them have)."""
        n = len(self.boxes)
        return min(n, int(self.share * n + 1e-6))

    def halted(self) -> bool:
        """True once the administrator paused searching (looked at now and then)."""
        if self.stopped is None and self.stop is not None:
            now = time.monotonic()
            if now - self.checked >= STOP_CHECK_SECONDS:
                self.checked = now
                try:
                    self.stopped = self.stop()
                except Exception:              # stop_reason reads the switches safely; this is a guard
                    log.warning("Reading the stop switch failed", exc_info=True)
                if self.stopped:
                    log.warning("OpenStreetMap: stopping, %s", self.stopped)
                    self.cancel.set()
                    self.say(f"OpenStreetMap: stopped, {self.stopped}", "stopped")
        return self.stopped is not None

    def say(self, text: str, note: str = "") -> None:
        """A progress message about the map data, with the areas done of the search's."""
        if self.progress:
            self.progress(Step(text, MAP, self.areas_done(), len(self.boxes), note=note))

    def pause(self, seconds: float) -> None:
        """Wait `seconds` (a busy server cooling down), unless the search is stopped."""
        until = time.monotonic() + seconds
        while not self.halted() and time.monotonic() < until:
            time.sleep(min(0.5, max(0.0, until - time.monotonic())))

    def first(self) -> list[Part]:
        return ([(None, 1.0, 0)] if self.whole
                else [(b, 1 / len(self.boxes), 0) for b in self.boxes])

    def query(self, box: Box | None) -> str:
        return build_query(self.lat, self.lon, self.radius, self.keywords, box=box)

    def areas(self) -> None:
        # Parts finish out of order on several threads; the count is of the search's
        # areas, so it only goes forward.
        if len(self.boxes) > 1:
            split = ", some areas asked again in smaller pieces" if self.split and not self.again else ""
            self.say(f"OpenStreetMap: {self.again or 'searching the free map data'} "
                     f"({self.areas_done()} of {len(self.boxes)} areas done{split})",
                     "retry" if self.again else "split" if split else "")

    def said(self) -> Callable[[int, int], None]:
        def note(server: int, servers: int) -> None:
            # Only a search asked in one piece says which server it is on (a wide one
            # counts its areas instead).
            if self.whole:
                self.say(f"OpenStreetMap: {self.again or 'searching the free map data'} "
                         f"(server {server} of {servers})",
                         "retry" if self.again else "another" if server > 1 else "")
        return note

    def ask(self, n: int, box: Box | None, deadline: float) -> tuple[Any, list[str]]:
        # The query's text doesn't depend on the time left, so a part answered on an
        # earlier run (or round) today comes from the cache.
        budget = min(deadline, time.monotonic() + config.OVERPASS_PART_SECONDS)
        return _fetch(self.query(box), budget, first=n, said=self.said(),
                      cancel=self.cancel if self.stop else None)

    def split_before(self, box: Box | None) -> bool:
        """An earlier run today had to ask this part in quarters."""
        return box is not None and any(
            cache_get(_answer_key(self.query(q)), config.OVERPASS_CACHE_TTL_SECONDS) is not None
            for q in _quarters(box))

    def quarters(self, part: Part) -> list[Part]:
        box, share, depth = part
        self.split = True
        return [(q, share / 4, depth + 1) for q in _quarters(box or self.boxes[0])]

    def answered(self, part: Part, data: Any) -> None:
        self.done += 1
        self.share += part[1]
        new = []
        for lead in map(parse_element, data.get("elements", [])):
            if lead and lead.source_id not in self.found:
                self.found[lead.source_id] = lead
                new.append(lead)
        report_found(new)
        if data.get("remark"):
            log.warning("OpenStreetMap note: %s", data["remark"])
            note = "The map data service returned only part of its results (it was busy)."
            if note not in self.warnings:
                self.warnings.append(note)

    def failed(self, part: Part, exc: BaseException, todo: list[Part]) -> None:
        self.errors.append(str(exc))
        if part[2] < config.OVERPASS_SPLITS:
            # Smaller parts are what busy servers still answer: ask again, split.
            todo += self.quarters(part)
        else:
            self.missing.append(part)

    def run_round(self, todo: list[Part], seconds: float) -> None:
        """Ask for the parts in todo, config.OVERPASS_PARALLEL at a time and nearest the
        centre first, within `seconds`; parts no server answered for end up in
        self.missing."""
        deadline = time.monotonic() + seconds
        self.split = False
        self.areas()
        with ThreadPoolExecutor(max_workers=config.OVERPASS_PARALLEL,
                                thread_name_prefix=f"{THREAD_PREFIX}map area") as pool:
            running: dict[Any, Part] = {}
            while todo or running:
                if self.halted() or not self._start(pool, running, todo, deadline):
                    break
                finished, _ = wait(running, timeout=STOP_CHECK_SECONDS if self.stop else None,
                                   return_when=FIRST_COMPLETED)
                for future in finished:
                    part = running.pop(future)
                    try:
                        data, _ = future.result()
                    except Exception as exc:  # noqa: BLE001 - SourceError, or a thread that broke
                        self.failed(part, exc, todo)
                    else:
                        self.answered(part, data)
                    self.areas()
            if self.stopped:
                # Stopped: the parts not yet answered are left (their queries see the
                # stop within a second and ask no other server).
                self.missing += todo + list(running.values())
                todo.clear()

    def _start(self, pool: ThreadPoolExecutor, running: dict[Any, Part], todo: list[Part],
               deadline: float) -> bool:
        """Start the parts nearest the centre until config.OVERPASS_PARALLEL run; False
        when none is running."""
        while todo and len(running) < config.OVERPASS_PARALLEL:
            if deadline - time.monotonic() < MIN_SECONDS_LEFT:
                self.missing += todo
                self.errors.append("out of time before asking for every part")
                todo.clear()
                break
            part = todo.pop(min(range(len(todo)), key=lambda i: _distance(self.lat, self.lon, todo[i][0])))
            if part[2] < config.OVERPASS_SPLITS and self.split_before(part[0]):
                todo += self.quarters(part)          # go straight to the quarters
                continue
            running[pool.submit(self.ask, self.started, part[0], deadline)] = part
            self.started += 1
        return bool(running)

    def missing_share(self) -> float:
        return sum(share for _, share, _ in self.missing)


def search(lat: float, lon: float, radius_miles: float, keywords: Sequence[str] = (),
           progress: Callable[[str], None] | None = None,
           stats: dict[str, Any] | None = None,
           stop: Callable[[], str | None] | None = None) -> tuple[list[Lead], list[str]]:
    """Return (leads, warnings).

    One big query for a whole 30-mile circle is what the free public map servers
    most often refuse or time out on, so a wide search is cut into parts (a grid of
    boxes up to config.OVERPASS_PART_MILES wide), asked config.OVERPASS_PARALLEL at a
    time, each part starting at a different mirror to spread the load. A part that
    fails is asked again once as four smaller quarters. The first round gets
    config.OVERPASS_DEADLINE_SECONDS; parts still missing after it (busy or throttling
    servers) are asked again automatically, up to config.OVERPASS_RETRY_ROUNDS more
    rounds, each after a pause of config.OVERPASS_RETRY_PAUSE_SECONDS and with
    config.OVERPASS_RETRY_SECONDS of its own. Answers are cached, so a later round (or
    a re-run the same day) asks only for what is still missing. `stats`, when given,
    records how many parts had to be asked again.

    `stop` (config.stop_reason) is looked at every few seconds throughout: once it
    gives a reason (the administrator paused searching), nothing more is asked and
    Stopped is raised, holding what the parts that had answered found.

    Raises SourceError when no part answered, and PartialResult (holding what was
    found) when only some did.
    """
    run = _Parts(lat, lon, radius_miles, keywords, progress, stop)
    run.run_round(run.first(), config.OVERPASS_DEADLINE_SECONDS)
    retried = 0
    for n in range(1, config.OVERPASS_RETRY_ROUNDS + 1):
        if not run.missing or run.stopped:
            break
        todo, run.missing = run.missing, []
        retried = max(retried, len(todo))
        log.info("OpenStreetMap: asking again for %d part(s) (round %d)", len(todo), n + 1)
        run.again = (f"asking again for the areas the busy map servers missed "
                     f"(try {n} of {config.OVERPASS_RETRY_ROUNDS})")
        run.say(f"OpenStreetMap: {run.again}, in a moment", "retry")
        run.pause(config.OVERPASS_RETRY_PAUSE_SECONDS)    # let a throttling server cool down
        if run.halted():
            run.missing = todo
            break
        run.run_round(todo, config.OVERPASS_RETRY_SECONDS)
    if stats is not None and retried:
        stats["osm areas asked again"] = (
            f"{retried} (all answered)" if not run.missing else f"{retried} (some didn't answer)")
    return _result(run)


def _result(run: _Parts) -> tuple[list[Lead], list[str]]:
    leads = list(run.found.values())
    if run.stopped:
        raise Stopped(run.stopped, leads, run.warnings)
    if not run.done:
        raise SourceError("All OpenStreetMap (Overpass) servers failed: " + " | ".join(run.errors))
    missing = run.missing_share()
    if missing > 0.001:
        share = max(1, round(missing * 100))
        n_boxes = len(run.boxes)
        answered = min(n_boxes - 1, round((1 - missing) * n_boxes))
        coverage = (f"about {answered} of {n_boxes} areas" if n_boxes > 1
                    else f"about {100 - share}% of the area")
        raise PartialResult(
            f"OpenStreetMap answered for only part of the area (about {share}% missing): "
            + " | ".join(run.errors[-5:]), leads, run.warnings, coverage,
            missing=run.missing, areas=n_boxes)
    return leads, run.warnings


def fill_in(lat: float, lon: float, radius_miles: float, keywords: Sequence[str],
            parts: Sequence[Part], seconds: float,
            stop: Callable[[], str | None] | None = None) -> tuple[list[Lead], list[Part]]:
    """Ask again, within `seconds`, for the parts of a search the map servers missed
    (PartialResult.missing): returns (the businesses found in the parts that answered
    now, the parts still missing). A part not yet split is asked in quarters if it
    fails, like in the search itself; nothing is raised when none answers. `stop` is
    looked at every few seconds, like in search(): once it gives a reason, nothing
    more is asked (the parts not answered stay missing)."""
    run = _Parts(lat, lon, radius_miles, keywords, None, stop)
    run.run_round(list(parts), seconds)
    return list(run.found.values()), run.missing
