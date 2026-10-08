"""Noticing that the live site went to sleep while the team works, and waking it ready.

The live site runs on Render's Starter plan (since 2026-10-07), which never sleeps. Until
then it was on the free plan, which stops the site after 15 minutes with no visit and
makes the next visitor wait 20 to 60 seconds; outside pingers kept it awake (the
operator runbook's "Keep the site awake").

If the site stops anyway, it notices: each start is recorded (when, and which version),
and a start during working hours with the same version as the start before it (no
deploy in between) means the site was restarted on Render (a setting saved, the plan
changed) or had been asleep (back on the free plan). That is reported under Recent problems and to the webhook, at most
once a Utah day, so the owner can check the plan.

Each start also gets the site ready for its first visitor, in the background: it
reaches the database (a free Neon database sleeps too) and reads the saved list once,
so the first page load doesn't wait for either.
"""

import logging
import threading
import time
import uuid
from collections.abc import Callable

from . import THREAD_PREFIX, alerts, saved, store
from .localtime import date_time_text, utah

# When the team would notice a wait: Utah working hours on working days. A start outside
# these (a weekend, an evening) bothers nobody, so it isn't reported.
WORK_HOURS = (7, 19)
WORK_DAYS = range(0, 5)           # Monday (0) to Friday (4)
# Nor is a start in the first minutes of the working day (on the free plan, the morning's
# first visit or ping is the one that wakes the site).
FIRST_PING_MINUTES = 20
# How the report starts (it is made at most once a Utah day).
SLEPT = "The site restarted"

log = logging.getLogger(__name__)
started_at = time.time()          # when this process started (/healthz shows it)


def check_start(version: str, at: float | None = None) -> str | None:
    """Record this start of the site (at, epoch seconds; version, the deployed commit) and
    return the problem it shows, reported (see above), or None."""
    at = time.time() if at is None else at
    when = utah(at)
    # "The site restarted at Oct 5, 2026, " (that Utah day's report)
    today = f"{SLEPT} at {date_time_text(at).rsplit(', ', 1)[0]}, "
    with store.connect() as db:
        last = db.one("SELECT version FROM site_starts ORDER BY at DESC LIMIT 1")
        db.run("INSERT INTO site_starts (id, at, version) VALUES (?, ?, ?)", (uuid.uuid4().hex, at, version))
        said = db.one("SELECT COUNT(*) FROM problems WHERE text LIKE ?", (f"{today}%",))
    minutes = when.hour * 60 + when.minute
    if when.weekday() not in WORK_DAYS or not WORK_HOURS[0] * 60 + FIRST_PING_MINUTES <= minutes < WORK_HOURS[1] * 60:
        return None
    if last is None or last[0] != version:
        return None                   # the first start, or a new version (a deploy)
    if said and said[0]:
        return None                   # already said today
    text = (f"{SLEPT} at {date_time_text(at)} Utah time without a new version. Saving a setting or "
            "changing the plan in Render restarts it like this, and then nothing needs doing. If "
            "nobody changed anything in Render, check that the service's Instance Type still says "
            "Starter (operator runbook, \"Keep the site awake\").")
    alerts.report("error", text)
    return text


def warm_up() -> None:
    """Reach the database and read the saved list once (its parsed copy is kept), so the
    first visitor's page doesn't wait for either."""
    saved.load()


def start(version: str) -> threading.Thread:
    """At the site's start: check_start (only with a version: on Render), then warm_up,
    in the background (a failure is only logged: the site works without either)."""
    def check() -> None:
        check_start(version)

    def run() -> None:
        steps: list[tuple[Callable[[], None], str]] = [(check, "Checking whether the site had been asleep")]
        for step, what in [*(steps if version else []), (warm_up, "Getting the saved list ready")]:
            try:
                step()
            except Exception:
                log.warning("%s failed", what, exc_info=True)

    thread = threading.Thread(target=run, daemon=True, name=f"{THREAD_PREFIX}wake-up")
    thread.start()
    return thread
