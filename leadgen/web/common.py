"""What every page of the website shares: its state, plain messages, saved-data loading."""

import logging
import threading
from urllib.parse import urlparse

from flask import current_app, request
from werkzeug.exceptions import ServiceUnavailable

from .. import calls, config, marks, saved, store, usage
from ..export import format_phone
from ..localtime import date_time_text
from ..pipeline import SearchParams
from ..scoring import TIER_LABELS

log = logging.getLogger("leadgen.web")

# What the pages say when something is down (plain words; the detail goes to the log).
DB_DOWN = "Can't reach your saved leads right now. Nothing is lost. Try again in a minute."
MARKS_DOWN = ("Couldn't load your Yes/No marks and calls right now. Nothing is lost. "
              "Please reload in a minute.")
SEARCH_PAUSED = ("Searching is paused by the administrator. Saved leads, calls and stats "
                 "still work.")
NOT_USED_UP = "Today's search was not used up."


class State:
    """One app's login settings and running searches (kept in app.extensions)."""

    def __init__(self, password, username):
        self.password, self.username = password, username
        self.failures = {}             # address -> times of recent wrong passwords
        self.jobs = {}                 # job id -> search job, oldest first
        self.lock = threading.Lock()

    def running_job(self):
        """The id of the search that is running now, or None."""
        with self.lock:
            return next((k for k, j in self.jobs.items() if j["state"] == "running"), None)


def state() -> State:
    return current_app.extensions["leadgen"]


class LoadError(Exception):
    """Saved data could not be read; the message is for the page."""


def db_message(exc):
    """The page's words for a database that can't be used."""
    if isinstance(exc, store.Unavailable) and "DATABASE_URL" in str(exc):
        return f"Saved leads are off: {exc}."          # a setup problem, for the owner
    return DB_DOWN


def with_marks_and_calls(leads):
    """Add each lead's mark and latest call, plus the undo offers; raises LoadError
    with a plain message when they can't be read (leads shown without their marks
    would all look unchecked)."""
    try:
        marks.apply(leads)
        calls.apply(leads)
        return {"mark": marks.pending_undos(), "call": calls.pending_undos()}
    except Exception as exc:
        log.error("Loading the marks or calls failed", exc_info=True)
        raise LoadError(MARKS_DOWN) from exc


def load_saved(uids=None):
    """Every saved lead (or with uids just those) with its mark and latest call, plus
    the undo offers, on one database connection. Raises LoadError with a plain
    message when any of it can't be read."""
    try:
        leads = saved.load(uids)
    except Exception as exc:
        log.error("Loading the saved leads failed", exc_info=True)
        raise LoadError(db_message(exc)) from exc
    return leads, with_marks_and_calls(leads)


def changed_uids(since):
    """The saved leads whose details, mark or calls changed after `since`."""
    try:
        return saved.changed_since(since) | marks.changed_since(since) | calls.changed_since(since)
    except Exception as exc:
        log.error("Checking for changes failed", exc_info=True)
        raise LoadError(db_message(exc)) from exc


def lead_json(lead, undo=None):
    undo = undo or {}
    return {
        "score": lead.score, "tier": lead.tier, "tier_label": TIER_LABELS.get(lead.tier, ""),
        "lead_type": lead.lead_type, "flags": lead.flags, "name": lead.name,
        "category": lead.category, "address": lead.address, "city": lead.city,
        "zip": lead.zip, "phone": format_phone(lead.phone), "website": lead.website,
        "distance": lead.distance_miles, "reasons": lead.reasons, "map_url": lead.map_url,
        "sources": lead.sources, "key": lead.uid, "has_baler": lead.has_baler,
        "call_outcome": lead.call_outcome, "call_notes": lead.call_notes,
        "call_count": lead.call_count, "last_call": date_time_text(lead.last_call_at),
        "last_call_at": lead.last_call_at,
        "undo_mark": undo.get("mark", {}).get(lead.uid),
        "undo_call": undo.get("call", {}).get(lead.uid),
    }


def same_origin():
    """Stop other websites from starting (billed) searches through this server."""
    site = request.headers.get("Sec-Fetch-Site")
    if site not in (None, "same-origin", "none"):
        return False
    origin = request.headers.get("Origin") or request.headers.get("Referer")
    return not origin or urlparse(origin).netloc == request.host


def wants_page():
    """A browser opening a page (not the page's own data requests)."""
    accept = request.accept_mimetypes
    return request.method == "GET" and accept["text/html"] > accept["application/json"]


def unavailable(text):
    """Raise a 503 whose message the error page shows as it is."""
    exc = ServiceUnavailable(text)
    exc.plain = True  # type: ignore[attr-defined]
    raise exc


def switches():
    """The administrator's off switches that are set right now (see config)."""
    return {"search_paused": config.switched_on(config.SEARCH_PAUSED_ENV),
            "google_off": config.switched_on(config.GOOGLE_OFF_ENV),
            "yelp_off": config.switched_on(config.YELP_OFF_ENV)}


def yelp_quota():
    """Yelp calls left for this site in the last 24 hours, or None when no Yelp key is set."""
    if not SearchParams().resolved_keys()[1]:
        return None
    budget = usage.yelp_budget()
    left = budget.left()
    if budget.problem:
        text = f"Yelp is paused: {budget.problem}."
    else:
        reset = budget.reset_text()
        text = (f"Yelp: {left} of today's {budget.limit} calls left"
                + (f" (resets at {reset})" if reset else ""))
    return {"left": left, "limit": budget.limit, "paused": bool(budget.problem), "text": text}
