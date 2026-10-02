"""What every page of the website shares: its state, plain messages, saved-data loading."""

import logging
import threading
from collections.abc import Iterable
from typing import Any, NoReturn
from urllib.parse import urlparse

from flask import current_app, request
from werkzeug.exceptions import ServiceUnavailable

from .. import calls, config, marks, places, saved, store, usage
from ..export import format_phone
from ..localtime import date_time_text
from ..models import Lead, Undo
from ..pipeline import EXEMPT_TYPES, SearchParams
from ..scoring import TIER_LABELS

log = logging.getLogger("leadgen.web")

# What the pages say when something is down (plain words; the detail goes to the log).
DB_DOWN = "Can't reach your saved leads right now. Nothing is lost. Try again in a minute."
MARKS_DOWN = ("Couldn't load your Yes/No marks and calls right now. Nothing is lost. "
              "Please reload in a minute.")
SEARCH_PAUSED = ("Searching is paused by the administrator. Saved leads, calls and stats "
                 "still work.")
NOT_USED_UP = "Today's search was not used up."

# The changes that can still be undone: {"mark": {uid: undo}, "call": {uid: undo}}.
Undos = dict[str, dict[str, Undo]]


class State:
    """One app's login settings and running searches (kept in app.extensions)."""

    def __init__(self, password: str, username: str, admin_password: str = "") -> None:
        self.password, self.username = password, username
        self.admin_password = admin_password         # locks the administrator's section
        self.failures: dict[str, list[float]] = {}   # address -> times of recent wrong passwords
        self.admin_failures: dict[str, list[float]] = {}
        self.jobs: dict[str, dict[str, Any]] = {}     # job id -> search job, oldest first
        self.lock = threading.Lock()

    def running_job(self) -> str | None:
        """The id of the search that is running now, or None."""
        with self.lock:
            return next((k for k, j in self.jobs.items() if j["state"] == "running"), None)


def state() -> State:
    s: State = current_app.extensions["leadgen"]
    return s


class LoadError(Exception):
    """Saved data could not be read; the message is for the page."""


def db_message(exc: BaseException) -> str:
    """The page's words for a database that can't be used."""
    if isinstance(exc, store.Unavailable) and "DATABASE_URL" in str(exc):
        return f"Saved leads are off: {exc}."          # a setup problem, for the owner
    return DB_DOWN


def with_marks_and_calls(leads: list[Lead]) -> Undos:
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


def load_saved(uids: Iterable[str] | None = None) -> tuple[list[Lead], Undos]:
    """Every saved lead (or with uids just those) with its mark and latest call, plus
    the undo offers, on one database connection. Raises LoadError with a plain
    message when any of it can't be read."""
    try:
        leads = saved.load(uids)
    except Exception as exc:
        log.error("Loading the saved leads failed", exc_info=True)
        raise LoadError(db_message(exc)) from exc
    return leads, with_marks_and_calls(leads)


def changed_uids(since: float) -> set[str]:
    """The saved leads whose details, mark or calls changed after `since`."""
    try:
        return saved.changed_since(since) | marks.changed_since(since) | calls.changed_since(since)
    except Exception as exc:
        log.error("Checking for changes failed", exc_info=True)
        raise LoadError(db_message(exc)) from exc


def lead_json(lead: Lead, undo: Undos | None = None) -> dict[str, Any]:
    undo = undo or {}
    # A listing without a city: the town (and ZIP) its map position is near, shown as
    # "near West Jordan, UT 84088" (places.py), so same-named rows can be told apart.
    # The listed city is tidied ("CLEARFIELD" -> "Clearfield", places.tidy_town).
    city = places.listed_town(lead)
    near = places.for_lead(lead) if not city else None
    return {
        "score": lead.score, "tier": lead.tier, "tier_label": TIER_LABELS.get(lead.tier, ""),
        "lead_type": lead.lead_type, "flags": lead.flags, "name": lead.name,
        # Competitors and Arco's own listing are flagged, never asked Yes / No.
        "prospect": lead.lead_type not in EXEMPT_TYPES,
        "closed": saved.is_closed(lead),
        "category": lead.category, "address": lead.address, "city": city,
        "zip": lead.zip, "near": near.text() if near else "",
        "phone": format_phone(lead.phone), "website": lead.website,
        "distance": lead.distance_miles, "reasons": lead.reasons, "map_url": lead.map_url,
        "sources": lead.sources, "key": lead.uid, "has_baler": lead.has_baler,
        "marked_by": lead.marked_by, "mark_clicks": lead.mark_clicks,
        "marks_disagreed": lead.marks_disagreed,
        "last_call_by": lead.last_call_by,
        "call_outcome": lead.call_outcome, "call_notes": lead.call_notes,
        "call_count": lead.call_count, "last_call": date_time_text(lead.last_call_at),
        "last_call_at": lead.last_call_at,
        "earlier_notes": lead.earlier_notes, "earlier_notes_when": date_time_text(lead.earlier_notes_at),
        "undo_mark": undo.get("mark", {}).get(lead.uid),
        "undo_call": undo.get("call", {}).get(lead.uid),
    }


def same_origin() -> bool:
    """Stop other websites from starting (billed) searches through this server."""
    site = request.headers.get("Sec-Fetch-Site")
    if site not in (None, "same-origin", "none"):
        return False
    origin = request.headers.get("Origin") or request.headers.get("Referer")
    return not origin or urlparse(origin).netloc == request.host


def wants_page() -> bool:
    """A browser opening a page (not the page's own data requests)."""
    accept = request.accept_mimetypes
    return request.method == "GET" and accept["text/html"] > accept["application/json"]


def unavailable(text: str) -> NoReturn:
    """Raise a 503 whose message the error page shows as it is."""
    exc = ServiceUnavailable(text)
    exc.plain = True  # type: ignore[attr-defined]
    raise exc


def switches() -> dict[str, bool]:
    """The administrator's off switches that are set right now (see config)."""
    return {"search_paused": config.switched_on(config.SEARCH_PAUSED_ENV),
            "google_off": config.switched_on(config.GOOGLE_OFF_ENV),
            "yelp_off": config.switched_on(config.YELP_OFF_ENV)}


def yelp_quota() -> dict[str, Any] | None:
    """Yelp calls left for this site in the last 24 hours, or None when no Yelp key is set."""
    if not SearchParams().resolved_keys()[1]:
        return None
    budget = usage.yelp_budget()
    left = budget.left()
    if budget.problem:
        text = f"Yelp is paused: {budget.problem}."
    else:
        # A rolling 24 hours: each call comes back 24 hours after it was made.
        reset = budget.reset_text()
        text = (f"Yelp: {left} of {budget.limit} calls left in the last 24 hours"
                + (f"; all back by {reset}" if reset and left < budget.limit else ""))
    return {"left": left, "limit": budget.limit, "paused": bool(budget.problem), "text": text}
