"""How each outside source is doing, for /healthz: set up or not, when it last
answered, when it last failed and why.

Every call to Yelp, Google, the map data (Overpass) and the place lookup goes through
http.request_json, which notes each attempt's outcome here; alerts.py notes each
alert delivered (by email or to the webhook). The notes go to the source_status table (one row per source and
outcome, replaced in place), so they survive a restart and every web worker sees the
same ones. A source's unchanged outcome is written at most once a minute, so a search
of a few hundred calls costs a handful of writes; a change of outcome is written at
once. Nothing here ever holds a key, an address or a request: a source is "set up"
or not, and a failure is its one-line reason (http.reason, keys hidden).
"""

import logging
import os
import threading
import time
from typing import Any

from . import config, mail, store, usage
from .localtime import date_time_text

log = logging.getLogger(__name__)

# The sources /healthz lists, in order, and how the page names each.
SOURCES = ("yelp", "google", "osm", "geocoding", "database", "alerts")
LABELS = {"yelp": "Yelp", "google": "Google Places", "osm": "Map data (OpenStreetMap)",
          "geocoding": "Place lookup", "database": "Database",
          "alerts": "Alerts (email or webhook)"}
WRITE_EVERY = 60.0            # a source's unchanged outcome is written at most this often
REASON_CHARS = 300

_lock = threading.Lock()
_recent: dict[str, tuple[float, bool]] = {}     # source -> (when last written, was it ok)


def record(source: str, ok: bool, reason: str = "") -> None:
    """Note that `source` answered (ok) or failed, with the failure's one-line reason.
    Never raises: a note that can't be written is logged and dropped."""
    now = time.time()
    with _lock:
        last_at, last_ok = _recent.get(source, (0.0, None))
        if last_ok is ok and now - last_at < WRITE_EVERY:
            return
        _recent[source] = (now, ok)
    reason = " ".join(reason.split())[:REASON_CHARS] if not ok else ""
    try:
        with store.connect() as db:
            db.run("INSERT INTO source_status (source, outcome, at, reason) VALUES (?, ?, ?, ?) "
                   "ON CONFLICT (source, outcome) DO UPDATE SET at = excluded.at, "
                   "reason = excluded.reason", (source, "ok" if ok else "failed", now, reason))
    except Exception as exc:  # noqa: BLE001 - a note must never break the call it is about
        log.info("Noting that %s %s failed: %s", source, "answered" if ok else "failed",
                 exc.__class__.__name__)


def forget() -> None:
    """Drop the in-memory throttle (tests, and the recorded check's throwaway database)."""
    with _lock:
        _recent.clear()


def _times(db: store.Db) -> dict[str, dict[str, tuple[float, str]]]:
    """{source: {"ok": (at, ""), "failed": (at, reason)}} from the table."""
    out: dict[str, dict[str, tuple[float, str]]] = {}
    for source, outcome, at, reason in db.all("SELECT source, outcome, at, reason FROM source_status"):
        out.setdefault(str(source), {})[str(outcome)] = (float(at), str(reason))
    return out


def _entry(times: dict[str, tuple[float, str]]) -> dict[str, Any]:
    ok = times.get("ok")
    failed = times.get("failed")
    return {"last_ok": date_time_text(ok[0]) if ok else None,
            "last_failure": date_time_text(failed[0]) if failed else None,
            "failure_reason": failed[1] if failed else ""}


def status() -> dict[str, Any]:
    """Every source's standing, for /healthz (public: never a key or an address).

    Each source: set_up (its key or address is set; the free sources always are), last_ok
    and last_failure (Utah time, or null), failure_reason (one line, or ""). Yelp and
    Google add switched_off (the administrator's switch) and Yelp calls_24h / calls_limit
    (the rolling budget). The database adds kind (postgres or sqlite) and ok (it answered
    just now). The alerts are set up when email (mail.py) or the webhook is; their last_ok
    is when a message was last delivered on either.
    """
    from .pipeline import SearchParams  # it imports the sources, which import http
    google_key, yelp_key, _ = SearchParams().resolved_keys()
    database, times = _database()
    out: dict[str, Any] = {}
    for source in SOURCES:
        entry = _entry(times.get(source, {}))
        if source == "yelp":
            entry.update(set_up=set_up(yelp_key, "YELP_API_KEY", config.YELP_OFF_ENV),
                         switched_off=config.switched_on(config.YELP_OFF_ENV), **_yelp_calls(yelp_key))
        elif source == "google":
            entry.update(set_up=set_up(google_key, "GOOGLE_PLACES_API_KEY", config.GOOGLE_OFF_ENV),
                         switched_off=config.switched_on(config.GOOGLE_OFF_ENV))
        elif source == "database":
            entry.update(database)
        elif source == "alerts":
            entry.update(set_up=bool(os.environ.get("LEADGEN_ALERT_WEBHOOK", "").strip())
                         or mail.set_up())
        else:
            entry["set_up"] = True      # the free services need no key
        out[source] = entry
    return out


def set_up(key: str, env: str, off_env: str) -> bool:
    """Whether a paid source has its key: the usable key, or (when the administrator
    switched the source off, which blanks the key) one set in the environment."""
    return bool(key) or (config.switched_on(off_env) and bool(os.environ.get(env, "").strip()))


def _database() -> tuple[dict[str, Any], dict[str, dict[str, tuple[float, str]]]]:
    """(the database's own entry, every source's recorded times): it answers now, or
    the reason it doesn't (then no times are known)."""
    entry: dict[str, Any] = {"set_up": not (store.on_render() and not store.database_url()),
                             "kind": "postgres" if store.database_url() else "sqlite", "ok": False,
                             "last_ok": None, "last_failure": None, "failure_reason": ""}
    times: dict[str, dict[str, tuple[float, str]]] = {}
    started = time.time()
    try:
        with store.connect() as db:
            times = _times(db)
        entry.update(ok=True, last_ok=date_time_text(time.time()))
    except Exception as exc:  # noqa: BLE001 - the whole point: say that it is down, and why
        log.warning("Reading the source status failed: %s", exc.__class__.__name__)
        if store.last_failure is None or store.last_failure[0] < started:
            # Not a connection failure (store.py would have noted that): say what it was.
            store.last_failure = (time.time(), f"{exc.__class__.__name__}: {store._reason(exc)}")
    failed = store.last_failure
    if failed is not None:
        entry.update(last_failure=date_time_text(failed[0]), failure_reason=failed[1][:REASON_CHARS])
    return entry, times


def _yelp_calls(yelp_key: str) -> dict[str, Any]:
    """Yelp's calls in the last 24 hours and its limit; the count is null when it can't be read."""
    out: dict[str, Any] = {"calls_limit": config.YELP_DAILY_LIMIT, "calls_24h": None}
    if not yelp_key:
        return out
    budget = usage.yelp_budget()
    used = budget.used()
    if not budget.problem:
        out["calls_24h"] = used
    return out
