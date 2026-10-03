"""Noticing that the live site went to sleep while the team works, and waking it ready.

Render's free plan stops the site after 15 minutes with no visit and starts it again
on the next one, which then waits 20 to 60 seconds. An outside pinger opens /healthz
every few minutes from before 6 AM to 9 PM Utah time (the operator runbook's "Keep the
site awake", an owner step), so that never happens while the team works. A GitHub
Actions job does the same as a backup, but GitHub starts scheduled jobs late or not
at all.

If the pinging stops, the site notices: each start is recorded (when, and which
version), and a start during working hours with the same version as the start
before it (no deploy in between) means the site had been asleep, or was restarted on
Render. That is reported under Recent problems and to the webhook, at most once a Utah
day, so the owner can check the pinger.

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

# Utah working hours (6 AM to 9 PM), when the site should never have to wake up.
WORK_HOURS = (6, 21)
# The pinger's first ping of the day may be the one that wakes the site: a start in the
# first minutes of the working day is expected.
FIRST_PING_MINUTES = 20
# How the report starts (it is made at most once a Utah day).
SLEPT = "The site had gone to sleep"

log = logging.getLogger(__name__)
started_at = time.time()          # when this process started (/healthz shows it)


def check_start(version: str, at: float | None = None) -> str | None:
    """Record this start of the site (at, epoch seconds; version, the deployed commit) and
    return the problem it shows, reported (see above), or None."""
    at = time.time() if at is None else at
    when = utah(at)
    # "The site had gone to sleep and started again at Oct 5, 2026, " (that Utah day's report)
    today = f"{SLEPT} and started again at {date_time_text(at).rsplit(', ', 1)[0]}, "
    with store.connect() as db:
        last = db.one("SELECT version FROM site_starts ORDER BY at DESC LIMIT 1")
        db.run("INSERT INTO site_starts (id, at, version) VALUES (?, ?, ?)", (uuid.uuid4().hex, at, version))
        said = db.one("SELECT COUNT(*) FROM problems WHERE text LIKE ?", (f"{today}%",))
    minutes = when.hour * 60 + when.minute
    if not WORK_HOURS[0] * 60 + FIRST_PING_MINUTES <= minutes < WORK_HOURS[1] * 60:
        return None
    if last is None or last[0] != version:
        return None                   # the first start, or a new version (a deploy)
    if said and said[0]:
        return None                   # already said today
    text = (f"{SLEPT} and started again at {date_time_text(at)} Utah time without a new version, so "
            "whoever opened it then waited up to a minute. The keep-awake pinger isn't reaching the site "
            "every few minutes: check it (operator runbook, \"Keep the site awake\").")
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
