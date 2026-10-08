"""The Find leads page's search history (GET /searches): each day's search in the
page's words, whether today's is used, a search a restart cut off, and, for an unlocked
administrator, the recent problems."""

import logging
import math
import time
from typing import Any

from flask import Blueprint, jsonify
from flask.typing import ResponseReturnValue

from .. import alerts, daily, fillin, interrupted
from ..localtime import clock_text
from .auth import admin_open, admin_state
from .common import SEARCH_PAUSED, db_message, state, switches, yelp_quota
from .search_words import plain_details
from .switch_routes import _switch_list

log = logging.getLogger("leadgen.web")
bp = Blueprint("history", __name__)


def _recover() -> float | None:
    """Finish the searches a server restart cut off (interrupted.recover); while one is
    still to be finished (its server is stopping, or has just stopped), about how many
    seconds until it can be (interrupted.wait), else None."""
    try:
        interrupted.recover()
        return interrupted.wait()
    except Exception:
        log.warning("Finishing a search a server restart cut off failed", exc_info=True)
        return None


@bp.get("/searches")
def searches() -> ResponseReturnValue:
    """Each day's search (when, what, what it found), and whether today's is used."""
    cut_off = _recover()
    try:
        body = daily.history()
    except Exception as exc:
        log.error("Loading the search history failed", exc_info=True)
        return jsonify({"error": db_message(exc)}), 503
    # How much of its area each search covered is said once, in the page's words
    # (fillin.coverage): the Find page's status, and one line of its Details; the notes
    # never repeat it, nor show internal counts. Copies, so today's record (in both
    # "current" and "searches") is worded once each, and the stored records stay as they are.
    paused = switches()["search_paused"]
    for key in ("current", "filling"):
        if body.get(key):
            body[key] = _for_page(body[key], paused, earlier=key == "filling")
    body["searches"] = [_for_page(r, paused) for r in body["searches"]]
    problems: dict[str, Any] | None = None           # only for an unlocked administrator
    unread = False
    if admin_open():
        try:
            problems = alerts.recent()
        except Exception:
            log.warning("Loading the recent problems failed", exc_info=True)   # the history is still worth showing
            unread = True
    body["problems"] = problems
    # Never an all-clear that wasn't read: the page says the problems couldn't be loaded.
    body["problems_unread"] = unread
    current = body.get("current")
    if current:
        # When an unfinished search stops holding the day, in Utah time like every time.
        current["free_at"] = clock_text(current["at"] + daily.STALE_SECONDS)
    running = state().running_job()
    # A search cut off by a restart, still being finished: the page says so, and when Find
    # leads is available again (it asks again then, and re-enables by itself).
    body["cut_off"] = cut_off is not None and running is None
    if body["cut_off"] and cut_off is not None:
        body["cut_off_wait"] = math.ceil(cut_off)
        body["cut_off_until"] = clock_text(time.time() + cut_off)
    return jsonify({**body, "yelp": yelp_quota(), "running": running,
                    "paused": SEARCH_PAUSED if switches()["search_paused"] else None,
                    "switches": _switch_list(), "admin": admin_state()})


def _for_page(record: dict[str, Any], paused: bool, earlier: bool = False) -> dict[str, Any]:
    """A day's search as the history shows it (see searches)."""
    record = dict(record)
    details: dict[str, Any] = record["details"] if isinstance(record.get("details"), dict) else {}
    record["coverage"] = fillin.coverage({**record, "details": details}, paused, earlier)
    if details or record["coverage"]:
        record["details"] = plain_details(details, record["coverage"])
    record["warnings"] = fillin.plain_notes(record.get("warnings") or [])
    if isinstance(record.get("reason"), str):
        record["reason"] = fillin.plain_reason(record["reason"])
        if record.get("interrupted"):
            record["reason"] = interrupted.plain_reason(record["reason"])
    return record
