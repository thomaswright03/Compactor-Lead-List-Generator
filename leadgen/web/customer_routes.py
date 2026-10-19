"""The Customers page's server side: the fingerprints of the company's current customers
(customers.py). The list itself is read in the browser and never sent here."""

import logging

from flask import Blueprint, abort, jsonify, request
from flask.typing import ResponseReturnValue

from .. import customers, saved
from ..localtime import date_time_text
from .common import db_message, same_origin

log = logging.getLogger("leadgen.web")
bp = Blueprint("customer_routes", __name__)


def _state() -> dict[str, object]:
    """The salt the page fingerprints with, the list on file and how many saved leads it hides."""
    salt = customers.salt()
    info = customers.info()
    hidden = 0
    if info["keys"]:
        _, hashes = customers.on_file()
        hidden = sum(customers.is_customer(l, salt, hashes) for l in saved.load())
    at = info["at"]
    return {**info, "salt": salt, "hidden": hidden,
            "when": date_time_text(float(at)) if isinstance(at, (int, float)) else ""}


@bp.get("/customers")
def customer_list() -> ResponseReturnValue:
    try:
        return jsonify(_state())
    except Exception as exc:
        log.error("Reading the current customers failed", exc_info=True)
        return jsonify({"error": db_message(exc)}), 503


@bp.post("/customers")
def put_customers() -> ResponseReturnValue:
    """Put a list on file in place of the one before: {"hashes": [...], "customers": n, "by"}."""
    if not same_origin():
        abort(403)
    data = request.get_json(silent=True) or {}
    hashes, count = data.get("hashes"), data.get("customers")
    if not isinstance(hashes, list) or not isinstance(count, int) or isinstance(count, bool):
        return jsonify({"error": customers.NOT_A_LIST}), 400
    try:
        customers.replace(hashes, count, str(data.get("by") or ""))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        log.error("Saving the current customers failed", exc_info=True)
        return jsonify({"error": db_message(exc)}), 503
    return customer_list()


@bp.post("/customers/clear")
def clear_customers() -> ResponseReturnValue:
    """Take the list off file: the leads it hid show again."""
    if not same_origin():
        abort(403)
    data = request.get_json(silent=True) or {}
    try:
        customers.clear(str(data.get("by") or ""))
    except Exception as exc:
        log.error("Clearing the current customers failed", exc_info=True)
        return jsonify({"error": db_message(exc)}), 503
    return customer_list()
