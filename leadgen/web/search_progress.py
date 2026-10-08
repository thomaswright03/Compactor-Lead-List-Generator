"""The progress bar of a running search on the Find leads page: the step it is on, how
far along it is (the percentage never goes back) and whether a step is taking longer than
usual (_Progress), over only the steps the search runs (skipped_steps)."""

import math
import time
from typing import Any

from ..pipeline import SearchParams
from ..progress import MAP, ORDER, PAID, Step
from .search_words import plain_progress

# A running or finished search, as the page follows it (state, step, pct, result...).
Job = dict[str, Any]

STEPS = ["Find the location", "Search Yelp and Google", "Search map data", "Merge and score",
         "Save"]
# A step running longer than this (seconds) is "taking longer than usual" on the page.
SLOW_SECONDS = [30, 240, 150, 60, 60]

# Each step's share of the bar (in progress.ORDER: locate, Yelp and Google, map data,
# merge, save), out of the steps the search runs.
STEP_WEIGHTS = (3.0, 47.0, 36.0, 8.0, 6.0)
PAID_STEP, MAP_STEP = ORDER.index(PAID), ORDER.index(MAP)
# While an area is awaited the bar creeps on through most of that area's share, about
# this fast (seconds), but never reaches the next area's mark before the area answers.
CREEP_SECONDS = 60.0
CREEP_PART = 0.9


class _Progress:
    """Turns the search's progress (progress.Step values: which step, how far it is)
    into the page's message, step and percentage.

    The bar spans only the steps the search runs: a step it skips (job["skipped"]:
    no Google or Yelp set up, say) has no share, and once past Yelp and Google their
    share is what they really did (calls made of the calls they could have made; none
    when they made none: out of calls, switched off, or answered from saved results),
    so a search of the free map data alone starts near 0 and its bar follows the areas
    done. Within the map step the bar moves with the areas that answered, plus a small
    creep within the area being waited for, never past the next area's mark. The
    percentage shown never goes back, and the "N of M areas done" count stays in the
    message until the map step ends.
    """

    def __init__(self, job: Job) -> None:
        self.job = job
        self.calls = 0                           # Yelp and Google calls made
        self.cap = 0                             # ... of the calls they may make
        self.map_started: float | None = None
        self.areas_done = self.areas_total = 0
        self.area_at: float | None = None        # when the latest area answered
        self.shown = 0.0
        self.step_started = time.time()
        job.update(step=0, pct=1.0, started=time.time())

    def slow(self) -> bool:
        step = self.job["step"]
        return step < len(SLOW_SECONDS) and time.time() - self.step_started > SLOW_SECONDS[step]

    def __call__(self, msg: str) -> None:
        job = self.job
        if not isinstance(msg, Step):
            job["message"] = plain_progress(msg)   # a note with no values: the bar stays
            return
        step = ORDER.index(msg.step)
        if msg.step == PAID:
            if msg.done:
                self.calls += 1
            else:
                self.cap += msg.total               # a source says how many calls it may make
            fraction = min(1.0, self.calls / self.cap) if self.cap else 0.0
        elif msg.step == MAP:
            self.map_started = self.map_started or time.time()
            # Parts finish on several threads, so a message can arrive after a newer
            # one: the count only moves forward.
            if msg.done > self.areas_done:
                self.area_at = time.time()
            self.areas_done = max(self.areas_done, msg.done)
            self.areas_total = max(self.areas_total, msg.total)
            msg = Step(msg, MAP, self.areas_done, self.areas_total, msg.source, msg.note)
            fraction = self.areas_done / self.areas_total if self.areas_total else 0.0
        else:
            fraction = msg.done / msg.total if msg.total else 0.0
        job["message"] = plain_progress(msg)
        if step > job["step"]:
            self.step_started = time.time()
        job["step"] = max(job["step"], step)
        job["pct"] = max(job["pct"], self._percent(step, fraction))

    def _weights(self) -> list[float]:
        """Each step's share of the bar: the steps that run; Yelp and Google's, once
        past them, by the share of their calls they made."""
        weights = list(STEP_WEIGHTS)
        for skipped in self.job.get("skipped", []):
            weights[skipped] = 0.0
        if self.job["step"] > PAID_STEP:
            weights[PAID_STEP] *= min(1.0, self.calls / self.cap) if self.cap else 0.0
        return weights

    def _percent(self, step: int, fraction: float) -> float:
        weights = self._weights()
        return 100 * (sum(weights[:step]) + weights[step] * fraction) / (sum(weights) or 1.0)

    def pct(self) -> float:
        pct = float(self.job["pct"])
        if self.job["step"] == MAP_STEP and self.map_started:
            total = max(self.areas_total, 1)
            waited = time.time() - (self.area_at or self.map_started)
            creep = CREEP_PART * (1 - math.exp(-waited / CREEP_SECONDS))
            pct = max(pct, self._percent(MAP_STEP, (min(self.areas_done, total) + creep) / total))
        self.shown = max(self.shown, pct)
        return self.shown


def skipped_steps(params: SearchParams) -> list[int]:
    """Steps a search won't run: Yelp and Google when neither is set up (or both are
    switched off), map data when a paid source alone was picked."""
    google, yelp_key, _ = params.resolved_keys()
    paid = (params.source in ("google", "both", "yelp")
            or (params.source == "auto" and (google or yelp_key)))
    return ([] if paid else [1]) + ([] if params.source in ("osm", "both", "auto") else [2])
