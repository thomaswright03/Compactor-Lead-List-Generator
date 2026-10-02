"""A search cut off by a server restart keeps what it found and gives the day back.

The site redeploys whenever a change lands on main (render.yaml), and a restart
stops a running search with it. So while a search runs, what its sources return
(each Google or Yelp results page, each map area: sources.report_found) is
written to the database every HEARTBEAT_SECONDS (search_found), and its row in
search_runs says it is still alive. A search that ends, however it ends, removes
its rows: it saves what it found itself.

A run whose server stopped is finished by the next page that loads the search
history or starts a search (recover): its row says the server shut down (the
process marks its runs when it exits), or it has not said it is alive for
STALE_SECONDS (the process was killed). What it had found is merged, scored and
saved like a search's own, and the day's search is given back at once, with a
history row that says what happened ("Interrupted by a server restart: the 128
businesses it had found were saved."). Nothing is searched again by itself:
running the search again (and spending paid lookups on it) is the person's choice.
"""

import atexit
import json
import logging
import threading
import time
import uuid
from dataclasses import asdict, fields
from typing import Any

from . import THREAD_PREFIX, daily, saved, store
from .models import Lead
from .pipeline import SearchParams, finish_leads

log = logging.getLogger(__name__)

# A running search says it is alive (and writes what it found since) this often...
HEARTBEAT_SECONDS = 10.0
# ...and one that has not for this long lost its server.
STALE_SECONDS = 45.0

_FIELDS = {f.name for f in fields(Lead)}
CLOSED = "CLOSED_PERMANENTLY"

# This process's running searches, by id: their server is this one, so they are
# never finished here as interrupted.
_live: dict[str, "Run"] = {}
_live_lock = threading.Lock()


def _info(params: SearchParams) -> dict[str, Any]:
    """What finishing the search needs: where, how far, the words, what to keep."""
    return {"center": list(params.center) if params.center else None, "place": params.place,
            "location": params.location, "radius": params.radius_miles,
            "keywords": list(params.keywords), "min_score": params.min_score,
            "only_keyword_matches": params.only_keyword_matches, "limit": params.limit}


def _params(info: dict[str, Any]) -> SearchParams:
    center = info.get("center")
    return SearchParams(location=str(info.get("location") or ""),
                        radius_miles=float(info["radius"]), keywords=list(info["keywords"]),
                        min_score=int(info["min_score"]),
                        only_keyword_matches=bool(info.get("only_keyword_matches")),
                        include_closed=True, limit=int(info.get("limit") or 0),
                        center=(float(center[0]), float(center[1])) if center else None,
                        place=str(info.get("place") or ""))


class Run:
    """A running search's record: start() when it starts, add() what its sources
    return, end() when it ends, however it ends. Writing it never fails the search
    (without it a restart loses what the search found, as it used to)."""

    def __init__(self, day: str, params: SearchParams) -> None:
        self.id = uuid.uuid4().hex
        self.day, self.params = day, params
        self._found: list[Lead] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._beating: threading.Thread | None = None

    def start(self) -> None:
        if self.params.center is None:
            return                  # the site always knows where by now; nothing to finish with
        try:
            with store.connect() as db:
                row = db.one("SELECT at FROM searches WHERE day = ?", (self.day,))
                if row is None:
                    return
                db.run("INSERT INTO search_runs (id, day, started, alive, info) "
                       "VALUES (?, ?, ?, ?, ?)",
                       (self.id, self.day, float(row[0]), time.time(),
                        json.dumps(_info(self.params))))
        except Exception:
            log.warning("Recording the running search failed", exc_info=True)
            return
        with _live_lock:
            _live[self.id] = self
        self._beating = threading.Thread(target=self._beat, daemon=True,
                                         name=f"{THREAD_PREFIX}search heartbeat {self.day}")
        self._beating.start()

    def add(self, leads: list[Lead]) -> None:
        """What a source just returned (written on the next heartbeat)."""
        with self._lock:
            self._found += leads

    def _beat(self) -> None:
        while not self._stop.wait(HEARTBEAT_SECONDS):
            self.write()

    def write(self, alive: float | None = None) -> None:
        """Write what was found since the last time, and that the search is alive
        (alive=0: its server is shutting down)."""
        with self._lock:
            batch, self._found = self._found, []
        try:
            with store.connect() as db:
                if batch:
                    db.run("INSERT INTO search_found (id, run, at, leads) VALUES (?, ?, ?, ?)",
                           (uuid.uuid4().hex, self.id, time.time(),
                            json.dumps([asdict(lead) for lead in batch], separators=(",", ":"))))
                db.run("UPDATE search_runs SET alive = ? WHERE id = ?",
                       (time.time() if alive is None else alive, self.id))
        except Exception:
            log.warning("Writing the running search's progress failed", exc_info=True)
            with self._lock:
                self._found = batch + self._found       # tried again on the next heartbeat

    def end(self) -> None:
        """The search ended and saved what it found itself: its rows go."""
        self._stop.set()
        if self._beating is not None:
            self._beating.join(timeout=HEARTBEAT_SECONDS)
        with _live_lock:
            if _live.pop(self.id, None) is None:
                return
        try:
            with store.connect() as db:
                db.run("DELETE FROM search_found WHERE run = ?", (self.id,))
                db.run("DELETE FROM search_runs WHERE id = ?", (self.id,))
        except Exception:
            # Left behind, the run reads as interrupted later; recover() then finds the
            # day's search finished and only removes the rows.
            log.warning("Removing the finished search's progress failed", exc_info=True)


def _shutting_down() -> None:
    """The process is exiting (a deploy or restart stops it): its running searches say
    so, with what they found since the last heartbeat, so the next page finishes them
    at once instead of after STALE_SECONDS."""
    with _live_lock:
        runs = list(_live.values())
    for run in runs:
        run._stop.set()
        run.write(alive=0.0)


# Python's threading exit hooks run before it waits for the map search's worker threads
# (an area's request can take a minute or more), so the search says it ended at once;
# atexit is the fallback where they don't exist.
_on_exit = getattr(threading, "_register_atexit", None)
(_on_exit or atexit.register)(_shutting_down)


def _stale(alive: float, now: float) -> bool:
    return alive <= 0 or now - alive > STALE_SECONDS


def pending() -> bool:
    """True while a search another server ran is still to be finished: its server is
    shutting down or has just stopped, and it has not been finished yet."""
    with store.connect() as db:
        rows = db.all("SELECT id, alive FROM search_runs")
    return any(run_id not in _live for run_id, _ in rows)


def recover() -> int:
    """Finish the searches whose server stopped mid-search (see the module's doc);
    returns how many. Each is finished by one page only."""
    now = time.time()
    with store.connect() as db:
        rows = db.all("SELECT id, day, started, alive, info FROM search_runs")
    done = 0
    for run_id, day, started, alive, info in rows:
        if run_id in _live or not _stale(alive, now):
            continue
        with store.connect() as db:
            # Taken by this page; if it stops halfway the run goes stale again and the
            # next page finishes it.
            taken = db.one("UPDATE search_runs SET alive = ? WHERE id = ? AND alive = ? "
                           "RETURNING id", (now, run_id, alive))
        if taken is None:
            continue
        _finish(run_id, day, float(started), json.loads(info))
        done += 1
    return done


def _finish(run_id: str, day: str, started: float, info: dict[str, Any]) -> None:
    with store.connect() as db:
        record = db.one("SELECT at, info FROM searches WHERE day = ?", (day,))
        batches = db.all("SELECT leads FROM search_found WHERE run = ? ORDER BY at", (run_id,))
    # Still the day's unfinished search (it may have finished just before its server stopped).
    if record is not None and abs(float(record[0]) - started) < 1e-3 \
            and "leads" not in json.loads(record[1]):
        found = [Lead(**{k: v for k, v in data.items() if k in _FIELDS})
                 for (text,) in batches for data in json.loads(text)]
        params = _params(info)
        stats: dict[str, object] = {}
        for source in sorted({lead.source for lead in found}):
            stats[f"{source} raw results"] = sum(lead.source == source for lead in found)
        leads = (finish_leads(found, params.center, params, params.keywords, stats)
                 if found and params.center else [])
        new = saved.save_search(leads, params.keywords)[0] if leads else 0
        shown = sum(lead.business_status != CLOSED for lead in leads)
        log.warning("Search %s was cut off by a server restart: %d listings found, %d "
                    "businesses saved (%d new); the day is given back", day, len(found), shown, new)
        daily.release(day, reason(shown, bool(found)),
                      {"interrupted": True, "leads": shown, "new": new, "details": stats,
                       "found_near": params.place})
    with store.connect() as db:
        db.run("DELETE FROM search_found WHERE run = ?", (run_id,))
        db.run("DELETE FROM search_runs WHERE id = ?", (run_id,))


def reason(shown: int, found: bool) -> str:
    """The search history's line for a search a server restart cut off."""
    if shown:
        return (f"Interrupted by a server restart (the site was updated or restarted): the "
                f"{shown:,} business{'' if shown == 1 else 'es'} it had found "
                f"{'was' if shown == 1 else 'were'} saved.")
    if found:
        return ("Interrupted by a server restart (the site was updated or restarted): none of "
                "the places it had found were within the radius and good enough to keep.")
    return ("Interrupted by a server restart (the site was updated or restarted) before it "
            "found any businesses.")
