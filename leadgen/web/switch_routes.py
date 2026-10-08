"""The emergency switches on the Find leads page (POST /switches): pause searching, stop
using Google or Yelp, from the administrator's section, with no restart."""

import logging
from typing import Any

from flask import Blueprint, abort, jsonify, request
from flask.typing import ResponseReturnValue

from .. import switches as site_switches
from ..localtime import date_time_text
from .auth import admin_open, admin_refusal
from .common import db_message, same_origin

log = logging.getLogger("leadgen.web")
bp = Blueprint("switch_routes", __name__)


def _switch_list() -> dict[str, Any]:
    """The emergency switches for the page, each with when it was flipped in Utah time."""
    out = site_switches.state()
    for item in out.values():
        item["when"] = date_time_text(item.pop("at")) if item["site"] else ""
    return out


@bp.post("/switches")
def flip_switch() -> ResponseReturnValue:
    """Flip an emergency switch inside the site: it takes effect on the next request (a
    running search stops within seconds), with no restart."""
    if not same_origin():
        abort(403)
    if not admin_open():
        return admin_refusal()
    data = request.get_json(silent=True) or {}
    name = site_switches.KEYS.get(str(data.get("key") or ""))
    if name is None or not isinstance(data.get("on"), bool):
        return jsonify({"error": "Unknown switch."}), 400
    try:
        site_switches.set_switch(name, data["on"], str(data.get("by") or ""))
    except Exception as exc:
        log.error("Saving a switch failed", exc_info=True)
        return jsonify({"error": db_message(exc)}), 503
    return jsonify({"switches": _switch_list()})
