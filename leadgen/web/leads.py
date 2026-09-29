"""The saved list's server side: leads, Yes / No marks, calls, stats and downloads."""

import logging
import math
import time

from flask import Blueprint, Response, abort, jsonify, request

from .. import calls, config, marks, stats
from ..export import to_csv_bytes, to_xlsx_bytes
from ..localtime import date_time_text
from .common import (
    MARKS_DOWN,
    LoadError,
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


@bp.get("/leads")
def saved_leads():
    """Every saved lead, for the page to show before any search.

    With ?since=<the "now" of an earlier answer> only the leads whose details,
    mark or calls changed since then come back (and "removed": the ones that
    left the list), so keeping an open page up to date costs little however
    long the list grows.
    """
    now = time.time()
    since = request.args.get("since", type=float)
    try:
        if since is None or not math.isfinite(since) or since <= 0:
            leads, undo = load_saved()
            return jsonify({"leads": [lead_json(lead, undo) for lead in leads], "now": now})
        uids = changed_uids(since - SINCE_OVERLAP)
        leads, undo = load_saved(uids) if uids else ([], {})
    except LoadError as exc:
        return jsonify({"error": str(exc)}), 503
    kept = {lead.uid for lead in leads}
    return jsonify({"leads": [lead_json(lead, undo) for lead in leads], "changes": True,
                    "removed": sorted(uids - kept), "now": now})


@bp.post("/calls")
def log_call():
    """Record a call to a lead: its outcome and the conversation notes (kept for good)."""
    if not same_origin():
        abort(403)
    data = request.get_json(silent=True) or {}
    uid = str(data.get("key") or "")
    if not uid or len(uid) > 64:
        return jsonify({"error": "Unknown lead"}), 400
    try:
        call = calls.log_call(uid, str(data.get("outcome") or ""), str(data.get("notes") or ""))
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
def call_history(uid):
    try:
        return jsonify({"calls": calls.history(uid[:64])})
    except Exception as exc:
        log.error("Loading a call history failed", exc_info=True)
        return jsonify({"error": f"Couldn't load the calls. {db_message(exc)}"}), 503


@bp.get("/stats")
def stats_data():
    try:
        leads, _ = load_saved()
    except LoadError as exc:
        return jsonify({"error": str(exc)}), 503
    return jsonify({**stats.summarize(leads), "saved": len(leads),
                    "min_score": config.DEFAULT_MIN_SCORE})


@bp.post("/mark")
def mark():
    """Save whether a business has a baler ("yes" or "no"). A mark is kept for good:
    it can be switched but not cleared (except by undoing a click within
    marks.UNDO_SECONDS), and later searches keep it with the business."""
    if not same_origin():
        abort(403)
    data = request.get_json(silent=True) or {}
    uid, value = str(data.get("key") or ""), str(data.get("value") or "")
    if not uid or len(uid) > 64 or value not in marks.VALUES:
        return jsonify({"error": "Bad mark"}), 400
    try:
        undo = marks.set_mark(uid, value)
    except ValueError:
        return jsonify({"error": "That business isn't in the saved list. Reload the "
                                 "page."}), 404
    except Exception as exc:
        log.error("Saving a mark failed", exc_info=True)
        # The page already says the answer wasn't saved; this says why and what to do.
        return jsonify({"error": db_message(exc)}), 503
    return jsonify({"ok": True, "undo": undo, "now": time.time()})


def _undo(fn, what):
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
def undo_mark():
    """Undo a Yes/No click made in the last few minutes (a misclick), by its id."""
    return _undo(marks.undo, "mark")


@bp.post("/calls/undo")
def undo_call():
    """Undo a call saved in the last few minutes (a misclick)."""
    return _undo(calls.undo, "call")


@bp.get("/download/<job_id>.<fmt>")
def download(job_id, fmt):
    """A search's leads, or with job id "saved" every saved lead."""
    if fmt not in ("csv", "xlsx"):
        abort(404)
    if job_id == "saved":
        try:
            leads, _ = load_saved()
        except LoadError as exc:
            unavailable(str(exc))
        info = {"list": "All saved leads", "leads": len(leads)}
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
    if fmt == "csv":
        return Response(to_csv_bytes(leads), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=compactor-leads.csv"})
    data = to_xlsx_bytes(leads, info)
    return Response(data, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": "attachment; filename=compactor-leads.xlsx"})
