"""Checking that each outside source answers: `python -m leadgen check-sources`, and the
administrator's "Check sources" on the Find leads page.

Each source gets one cheapest real call: Yelp's is taken from the site's daily budget
first (and refused when the calls are used up), Google's is its IDs-only text search,
the map data and the place lookup are asked for one thing near AARCO's shop, and the
database is asked for a count. A source whose key isn't set is reported as not set up
and never called. Each answer is noted for /healthz like any other call (health.py).

With --recorded, the real Yelp and Google client code (headers, paging, parsing,
retries) runs against a local stand-in serving recorded answers (recorded.py), in a
throwaway database and cache, so the whole path can be seen working with no key.
"""

import os
import time
from collections.abc import Callable
from dataclasses import dataclass

from . import alerts, config, health, mail, store
from .http import HttpError, request_json
from .pipeline import SearchParams
from .sources import SourceError, google_places, yelp

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
# A tiny Overpass query: at most one map element from the block around AARCO's shop.
_LAT, _LON = config.OWN_COORDS
OVERPASS_QUERY = (f"[out:json][timeout:25];node({_LAT - 0.005:.4f},{_LON - 0.005:.4f},"
                  f"{_LAT + 0.005:.4f},{_LON + 0.005:.4f});out 1;")
NOT_IN_RECORDED = "Not checked with --recorded; run the check without it"


@dataclass
class Check:
    """One row of the table: the source, whether it is set up, whether it answered
    (None: it wasn't called), how long the call took, and what happened in plain words."""
    source: str
    set_up: bool
    ok: bool | None
    seconds: float | None
    detail: str

    @property
    def label(self) -> str:
        return health.LABELS[self.source]


def _timed(source: str, set_up: bool, call: Callable[[], str]) -> Check:
    """Run one check, noting how long it took; a SourceError or HttpError is the failure's
    words, anything else its class and words."""
    started = time.monotonic()
    try:
        detail = call()
    except (SourceError, HttpError) as exc:
        return Check(source, set_up, False, time.monotonic() - started, str(exc))
    except Exception as exc:  # noqa: BLE001 - a check reports; it never fails the command
        return Check(source, set_up, False, time.monotonic() - started, f"{exc.__class__.__name__}: {exc}")
    return Check(source, set_up, True, time.monotonic() - started, detail)


def _paid(source: str, key: str, env: str, off_env: str, call: Callable[[str], str]) -> Check:
    """A paid source: not called without its key, nor when the administrator switched it
    off (which blanks the key, so the environment is asked whether one is set)."""
    if not health.set_up(key, env, off_env):
        return Check(source, False, None, None, f"{env} is not set")
    if not key:
        return Check(source, True, None, None, "Switched off by the administrator: not called")
    return _timed(source, True, lambda: call(key))


def _osm() -> str:
    errors = []
    for endpoint in config.OVERPASS_ENDPOINTS:
        try:
            data = request_json("POST", endpoint, data={"data": OVERPASS_QUERY}, timeout=(10, 30),
                                retries=1, use_cache=False)
        except HttpError as exc:
            errors.append(str(exc))
            continue
        host = endpoint.split("/")[2]
        return f"{host} answered: {len(data.get('elements') or [])} map element near AARCO's shop"
    raise SourceError("No map server answered: " + "; ".join(errors))


def _geocoding() -> str:
    data = request_json("GET", NOMINATIM_URL, params={"q": "Salt Lake City, UT", "format": "json",
                                                      "countrycodes": "us", "limit": 1},
                        timeout=20, retries=1, use_cache=False)
    if not data:
        raise SourceError("The place lookup answered, but found nothing for Salt Lake City")
    return f"Found {data[0].get('display_name', 'Salt Lake City')}"


def _database() -> str:
    kind = "Postgres (DATABASE_URL)" if store.database_url() else "a local SQLite file"
    with store.connect() as db:
        row = db.one("SELECT COUNT(*) FROM leads")
    return f"{kind}: {int(row[0]) if row else 0:,} saved leads"


def _alerts() -> Check:
    entry = health.status()["alerts"]
    if not entry["set_up"]:
        return Check("alerts", False, None, None,
                     f"Neither email ({mail.USER_ENV} and {mail.PASSWORD_ENV}) nor a webhook "
                     f"({alerts.WEBHOOK_ENV}) is set")
    channels = [f"email to {mail.recipient()}"] if mail.set_up() else []
    if os.environ.get(alerts.WEBHOOK_ENV, "").strip():
        channels.append("the webhook")
    when = (f"last delivered {entry['last_ok']}" if entry["last_ok"] else "nothing delivered yet")
    return Check("alerts", True, None, None,
                 f"Set up ({' and '.join(channels)}); {when}. "
                 "`python -m leadgen test-alert` sends a test message")


def run_all(recorded: bool = False) -> list[Check]:
    """Every source's check, in the order /healthz lists them."""
    if recorded:
        return run_recorded()
    google_key, yelp_key, _ = SearchParams().resolved_keys()
    return [
        _paid("yelp", yelp_key, "YELP_API_KEY", config.YELP_OFF_ENV, yelp.check),
        _paid("google", google_key, "GOOGLE_PLACES_API_KEY", config.GOOGLE_OFF_ENV, google_places.check),
        _timed("osm", True, _osm),
        _timed("geocoding", True, _geocoding),
        _timed("database", not (store.on_render() and not store.database_url()), _database),
        _alerts(),
    ]


def run_recorded() -> list[Check]:
    """Yelp and Google through the recorded stand-in (recorded.py); the rest not checked."""
    from . import recorded
    set_up = {source: entry["set_up"] for source, entry in health.status().items()}
    rows = {source: Check(source, True, ok, seconds, detail)
            for source, (ok, seconds, detail) in recorded.run().items()}
    return [rows.get(source) or Check(source, bool(set_up.get(source)), None, None, NOT_IN_RECORDED)
            for source in health.SOURCES]


def table(checks: list[Check]) -> str:
    """The checks as a plain table (one line each)."""
    rows = [("Source", "Set up?", "Answered?", "Took", "What happened")]
    for c in checks:
        rows.append((c.label, "yes" if c.set_up else "no",
                     "-" if c.ok is None else ("yes" if c.ok else "NO"),
                     "-" if c.seconds is None else f"{c.seconds:.1f} s", c.detail))
    widths = [max(len(r[i]) for r in rows) for i in range(4)]
    return "\n".join("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(r[:4])) + "  " + r[4]
                     for r in rows)


def failed(checks: list[Check]) -> list[Check]:
    return [c for c in checks if c.ok is False]
