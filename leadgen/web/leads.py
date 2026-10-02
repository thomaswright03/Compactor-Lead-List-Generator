"""The saved list's server side: leads, Yes / No marks, calls, stats and downloads."""

import logging
import math
import time
from collections.abc import Callable
from typing import Any

from flask import Blueprint, Response, abort, jsonify, request
from flask.typing import ResponseReturnValue

from .. import calls, config, daily, marks, places, reference, saved, stats, store
from ..export import saved_list_info, to_csv_bytes, to_xlsx_bytes
from ..localtime import date_time_text
from ..models import Lead
from ..pipeline import EXEMPT_TYPES
from .auth import admin_open
from .common import (
    MARKS_DOWN,
    LoadError,
    Undos,
    changed_uids,
    db_message,
    lead_json,
    load_saved,
    same_origin,
    state,
    unavailable,
)

log = logging.getLogger("leadgen.web")

# A page asking "what changed since" looks back this much further (seconds), so a
# change stamped just before the previous answer but saved just after it (a big
# search's save takes a while) is never missed. Sending a lead twice is harmless.
SINCE_OVERLAP = 120.0

bp = Blueprint("leads", __name__)


# The page's views of the saved list (its tabs): prospects not yet checked, marked
# Yes, marked No; competitors and Arco's own listing (never asked Yes / No);
# businesses closed for good; every lead; and the businesses with a call logged
# (the Calls page). A closed business keeps its mark (so it stays under Yes or No,
# flagged) but is no longer offered for checking.
VIEWS = ("unchecked", "yes", "no", "competitors", "closed", "all", "called")
# Column sorts: the value to sort by. Equal values keep the saved order (best first).
SORTS = {
    "score": lambda l: l.score,
    "name": lambda l: l.name.lower(),
    "city": lambda l: (places.town_of(l) or "~").lower(),
    "miles": lambda l: 1e9 if l.distance_miles is None else l.distance_miles,
    "called": lambda l: l.last_call_at or 0,
}
MAX_LIMIT = 5000
# Rows in one answer when the page doesn't say: the list grows with every search and
# is kept for good, so an answer is always one page of it ("Show more" asks for the next).
PAGE_SIZE = 100


def is_prospect(lead: Lead) -> bool:
    return lead.lead_type not in EXEMPT_TYPES


def in_view(lead: Lead, view: str) -> bool:
    if view == "all":
        return True
    if view == "competitors":
        return not is_prospect(lead)
    if view == "called":
        return bool(lead.call_count)
    if view == "closed":
        return saved.is_closed(lead)
    if view == "unchecked" and saved.is_closed(lead):
        return False
    return is_prospect(lead) and (lead.has_baler or "unchecked") == view


def matches(lead: Lead, q: str, tier: str, phone: bool = False, more: str = "") -> bool:
    """The page's filter box, tier choice and "Has phone" tick. Several words typed
    ("walmart layton") match a lead when each is somewhere in its name, town, ZIP,
    category, address, type or flags (or in `more`: on the Calls page, its calls'
    summaries, outcomes and callers), in any order and any case (q is lowercased)."""
    if tier and lead.tier != tier:
        return False
    if phone and not lead.phone.strip():
        return False
    text = " ".join([lead.name, places.town_of(lead), lead.zip, lead.category, lead.address,
                     lead.lead_type, " ".join(lead.flags)]).lower()
    if more:
        text += " " + more
    return all(word in text for word in q.split())


def view_counts(leads: list[Lead]) -> dict[str, Any]:
    """How many leads each tab holds (ignoring the filter), and calls by result."""
    counts = dict.fromkeys(VIEWS, 0)
    outcomes = dict.fromkeys(calls.OUTCOMES, 0)
    counts["all"] = len(leads)
    for lead in leads:                    # one pass, the same rules as in_view
        closed = saved.is_closed(lead)
        counts["closed"] += closed
        if lead.call_count:
            counts["called"] += 1
            if lead.call_outcome in outcomes:
                outcomes[lead.call_outcome] += 1
        if not is_prospect(lead):
            counts["competitors"] += 1
        elif lead.has_baler in ("yes", "no"):
            counts[lead.has_baler] += 1
        elif not closed:
            counts["unchecked"] += 1
    return {**counts, "outcomes": outcomes}


def _recent(leads: list[Lead], undo: Undos) -> list[dict[str, Any]]:
    """The leads with a mark or call that can still be undone (Recent changes)."""
    return [lead_json(l, undo) for l in leads
            if l.uid in undo.get("mark", {}) or l.uid in undo.get("call", {})]


@bp.get("/leads")
def saved_leads() -> ResponseReturnValue:
    """The saved list as the page shows it: one view (tab), filtered, sorted, and
    one page of its rows, plus every tab's count (exact, over the whole list). The
    page asks for the rows it shows and "Show more" for the next page, so opening it
    stays quick however long the list grows.

    ?tab= one of VIEWS (default all), q= filter text, tier=, phone=1, sort= / dir=,
    offset= (default 0) and limit= (default PAGE_SIZE, at most MAX_LIMIT): the rows
    from offset on; keep= uids shown even outside the tab (rows just marked, which
    stay put for a few seconds; on the first page only, and a refresh sends them in
    full too).

    The Calls page asks for tab=called: its filter also looks in the calls'
    summaries, outcomes and callers, outcome= keeps the businesses whose latest call
    went that way, and "call_counts" are its tabs' counts (every called business
    and each outcome, the filter applied), however many businesses were called.

    With ?since=<the "now" of an earlier answer> only the leads whose details, mark
    or calls changed since then come back: in full when they belong in the view
    asked about, as just {"key", "in_view": false} when they don't (so the page can
    drop them if it shows them), with "removed": the ones that left the list. When
    more leads changed than the page shows (`limit`, e.g. after a search touched
    them all), the answer is {"reload": true} with the counts instead, and the page
    asks for its rows again: a refresh never sends more than one page of rows.
    """
    now = time.time()
    since = request.args.get("since", type=float)
    view = request.args.get("tab", "all")
    if view not in VIEWS:
        view = "all"
    q = (request.args.get("q") or "").strip().lower()[:200]
    tier = request.args.get("tier") or ""
    phone = request.args.get("phone") == "1"
    outcome = request.args.get("outcome") or ""
    if outcome not in calls.OUTCOMES or view != "called":
        outcome = ""
    try:
        if since is None or not math.isfinite(since) or since <= 0:
            leads, undo = load_saved()
            text = _call_text(view, q)
        else:
            uids = changed_uids(since - SINCE_OVERLAP)
            if not uids:
                return jsonify({"leads": [], "changes": True, "removed": [], "now": now})
            leads, undo = load_saved()
            text = _call_text(view, q)
    except LoadError as exc:
        return jsonify({"error": str(exc)}), 503

    def filtered(l: Lead) -> bool:
        return ((not outcome or l.call_outcome == outcome)
                and matches(l, q, tier, phone, text.get(l.uid, "")))
    extra = {"call_counts": _call_counts(leads, q, text)} if view == "called" else {}
    if since is None or not math.isfinite(since) or since <= 0:
        return jsonify({**_page(leads, undo, view, filtered), **extra, "now": now})
    shown = [l for l in leads if in_view(l, view) and filtered(l)]
    body = {"changes": True, "total": len(shown), "counts": view_counts(leads),
            "recent": _recent(leads, undo), **extra, "now": now}
    changed = [l for l in leads if l.uid in uids]
    limit = request.args.get("limit", type=int) or PAGE_SIZE
    if len(changed) > max(1, min(limit, MAX_LIMIT)):
        return jsonify({**body, "reload": True, "leads": [], "removed": []})
    kept = {lead.uid for lead in changed}
    pinned = set((request.args.get("keep") or "").split(",")[:50]) - {""}
    wanted = ({l.uid for l in shown} | pinned) & kept
    return jsonify({**body, "removed": sorted(uids - kept),
                    "leads": [{**lead_json(l, undo), "in_view": in_view(l, view) and filtered(l)}
                              if l.uid in wanted
                              else {"key": l.uid, "in_view": False} for l in changed]})


def _call_text(view: str, q: str) -> dict[str, str]:
    """What the Calls page's filter also looks in (calls.search_text), when it is used."""
    if view != "called" or not q:
        return {}
    try:
        return calls.search_text()
    except Exception as exc:
        log.error("Reading the calls for the filter failed", exc_info=True)
        raise LoadError(db_message(exc)) from exc


def _call_counts(leads: list[Lead], q: str, text: dict[str, str]) -> dict[str, Any]:
    """The Calls page's tab counts: every called business the filter keeps, and how
    many of them each outcome holds (their latest call's)."""
    outcomes = dict.fromkeys(calls.OUTCOMES, 0)
    called = [l for l in leads if l.call_count and matches(l, q, "", False, text.get(l.uid, ""))]
    for lead in called:
        if lead.call_outcome in outcomes:
            outcomes[lead.call_outcome] += 1
    return {"all": len(called), "outcomes": outcomes}


def _page(leads: list[Lead], undo: Undos, view: str, filtered: Callable[[Lead], bool]) -> dict[str, Any]:
    offset = max(0, request.args.get("offset", type=int) or 0)
    keep = set((request.args.get("keep") or "").split(",")[:50]) - {""} if not offset else set()
    rows = [l for l in leads if (in_view(l, view) or l.uid in keep) and filtered(l)]
    sort = request.args.get("sort") or "score"
    if sort not in SORTS:
        sort = "score"
    default_desc = sort in ("score", "called")
    desc = {"asc": False, "desc": True}.get(request.args.get("dir") or "", default_desc)
    if sort != "score" or not desc:
        rows.sort(key=SORTS[sort], reverse=desc)      # stable: ties keep the best-first order
    elif view == "unchecked":
        # Among equal scores, the leads that can be phoned from the page come first.
        rows.sort(key=lambda l: (-l.score, not l.phone.strip()))
    limit = max(1, min(request.args.get("limit", type=int) or PAGE_SIZE, MAX_LIMIT))
    shown = rows[offset:offset + limit]
    return {"leads": [lead_json(l, undo) for l in shown], "total": len(rows), "offset": offset,
            "counts": view_counts(leads), "recent": _recent(leads, undo)}


# Every mark and call says who made it (so colleagues know whom to ask), as the page asks.
NO_NAME = ("Enter your name first (Your name, in the side bar, or under Menu on a phone), so the team can see "
           "who made this change. Nothing was saved.")


@bp.post("/calls")
def log_call() -> ResponseReturnValue:
    """Record a call to a lead: its outcome and the conversation notes (kept for good)."""
    if not same_origin():
        abort(403)
    data = request.get_json(silent=True) or {}
    uid = str(data.get("key") or "")
    if not uid or len(uid) > 64:
        return jsonify({"error": "Unknown lead"}), 400
    call_id = str(data["id"]) if data.get("id") else None
    if not store.person(data.get("by")):
        return jsonify({"error": NO_NAME}), 400
    try:
        call = calls.log_call(uid, str(data.get("outcome") or ""), str(data.get("notes") or ""),
                              call_id, str(data.get("by") or ""))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        log.error("Saving a call failed", exc_info=True)
        return jsonify({"error": db_message(exc)}), 503       # the page says it wasn't saved
    return jsonify({"ok": True, "now": time.time(),
                    "call": {**call, "when": date_time_text(call["at"]),
                             "undo": {"id": call["id"],
                                      "until": call["at"] + calls.UNDO_SECONDS}}})


@bp.get("/calls/<uid>")
def call_history(uid: str) -> ResponseReturnValue:
    try:
        return jsonify({"calls": calls.history(uid[:64]), "marks": marks.history(uid[:64])})
    except Exception as exc:
        log.error("Loading a call history failed", exc_info=True)
        return jsonify({"error": f"Couldn't load the calls. {db_message(exc)}"}), 503


@bp.get("/stats")
def stats_data() -> ResponseReturnValue:
    try:
        leads, _ = load_saved()
    except LoadError as exc:
        return jsonify({"error": str(exc)}), 503
    return jsonify({**stats.summarize(leads), "saved": len(leads),
                    "min_score": config.DEFAULT_MIN_SCORE})


@bp.post("/mark")
def mark() -> ResponseReturnValue:
    """Save whether a business has a baler ("yes" or "no"). A mark is kept for good:
    it can be switched but not cleared (except by undoing a click within
    marks.UNDO_SECONDS), and later searches keep it with the business."""
    if not same_origin():
        abort(403)
    data = request.get_json(silent=True) or {}
    uid, value = str(data.get("key") or ""), str(data.get("value") or "")
    if not uid or len(uid) > 64 or value not in marks.VALUES:
        return jsonify({"error": "Bad mark"}), 400
    if not store.person(data.get("by")):
        return jsonify({"error": NO_NAME}), 400
    try:
        undo = marks.set_mark(uid, value, str(data.get("by") or ""))
    except ValueError:
        return jsonify({"error": "That business isn't in the saved list. Reload the "
                                 "page."}), 404
    except Exception as exc:
        log.error("Saving a mark failed", exc_info=True)
        # The page already says the answer wasn't saved; this says why and what to do.
        return jsonify({"error": db_message(exc)}), 503
    return jsonify({"ok": True, "undo": undo, "now": time.time()})


def _undo(fn: Callable[[str], bool], what: str) -> ResponseReturnValue:
    if not same_origin():
        abort(403)
    change = str((request.get_json(silent=True) or {}).get("id") or "")
    if not change or len(change) > 64:
        return jsonify({"error": "Bad undo"}), 400
    try:
        ok = fn(change)
    except Exception as exc:
        log.error("Undoing a %s failed", what, exc_info=True)
        return jsonify({"error": db_message(exc)}), 503
    if not ok:
        return jsonify({"error": "It's too late to undo that (after 5 minutes, or once "
                                 "there's a newer change)."}), 409
    return jsonify({"ok": True})


@bp.post("/mark/undo")
def undo_mark() -> ResponseReturnValue:
    """Undo a Yes/No click made in the last few minutes (a misclick), by its id."""
    return _undo(marks.undo, "mark")


@bp.post("/calls/undo")
def undo_call() -> ResponseReturnValue:
    """Undo a call saved in the last few minutes (a misclick)."""
    return _undo(calls.undo, "call")


@bp.get("/download/scoring-reference.json")
def scoring_reference() -> ResponseReturnValue:
    """The scoring check file (tests/fixtures/scoring_reference.json) with every business
    marked Yes / No on the site in it (only the facts the scoring reads: no phone
    numbers, addresses or call notes), for the developer to commit (reference.py)."""
    if not admin_open():
        abort(403, "Unlock the administrator's section on the Find leads page first.")
    try:
        data, _, _ = reference.build()
    except Exception as exc:
        log.error("Building the scoring check file failed", exc_info=True)
        unavailable(db_message(exc))
    return Response(reference.dumps(data), mimetype="application/json", headers={
        "Content-Disposition": "attachment; filename=scoring_reference.json"})


@bp.get("/download/<job_id>.<fmt>")
def download(job_id: str, fmt: str) -> ResponseReturnValue:
    """A search's leads, or with job id "saved" every saved lead."""
    if fmt not in ("csv", "xlsx"):
        abort(404)
    if job_id == "saved":
        try:
            leads, _ = load_saved()
        except LoadError as exc:
            unavailable(str(exc))
        # When the first and latest search started, as the search history shows them;
        # leads saved without a search on record (the command line) go by when they were saved.
        searched = True
        try:
            first, latest = daily.search_times()
            if first is None:
                searched = False
                first, latest = saved.date_range()
        except Exception:
            log.warning("Reading the search dates failed", exc_info=True)
            first = latest = None                  # the file is still useful without them
        info = saved_list_info(leads, first, latest, searched)
    else:
        job = state().jobs.get(job_id)
        if not job or job["state"] != "done":
            abort(404)
        try:
            leads = calls.apply(marks.apply(job["result"].leads))
        except Exception:
            log.error("Loading marks for a download failed", exc_info=True)
            unavailable(MARKS_DOWN)
        info = job["result"].run_info(job["params"])
    # Named with the Utah date, so the copies people keep can be told apart.
    disposition = f"attachment; filename=compactor-leads-{daily.today()}.{fmt}"
    if fmt == "csv":
        return Response(to_csv_bytes(leads), mimetype="text/csv",
                        headers={"Content-Disposition": disposition})
    data = to_xlsx_bytes(leads, info)
    return Response(data, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": disposition})
