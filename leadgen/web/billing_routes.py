"""Sign-up mode's billing pages (billing.py): the owner's Choose a plan and Manage billing
buttons on Settings, the way back from Stripe, and Stripe's webhook. Without sign-up
mode none of them exist (404).

Choose a plan and Manage billing are the Settings page's plain form POSTs: each answers
with a redirect (303) to the Stripe page, or {"url": ...} when the request asks for JSON.
When billing isn't set up or Stripe can't be reached, the owner goes back to Settings,
which says so. The webhook is public (web/accounts.PUBLIC) and has no same-origin check
(Stripe's servers send it); its Stripe-Signature header proves it instead.
"""

import json
import logging

from flask import Blueprint, abort, jsonify, redirect, request, session
from flask.typing import ResponseReturnValue

from .. import accounts as account_store
from .. import billing, signup_mode
from ..company import Company
from .accounts import current_company, current_user, require_owner
from .common import same_origin

log = logging.getLogger("leadgen.web")
bp = Blueprint("billing", __name__)

# The largest webhook body read (Stripe's events are a few kilobytes).
MAX_EVENT_BYTES = 512 * 1024


def _only_in_signup_mode() -> None:
    if not signup_mode.on():
        abort(404)


def _back() -> str:
    """The site's own address, for Stripe to send the owner back to."""
    return request.host_url.rstrip("/")


def _wants_json() -> bool:
    accept = request.accept_mimetypes
    return request.is_json or accept["application/json"] > accept["text/html"]


def _go(url: str) -> ResponseReturnValue:
    return jsonify({"url": url}) if _wants_json() else redirect(url, 303)


def _not_now(text: str) -> ResponseReturnValue:
    if _wants_json():
        return jsonify({"error": text}), 503
    session["settings_note"] = text
    return redirect("/#settings", 303)


def _owner_company() -> Company:
    _only_in_signup_mode()
    if not same_origin():
        abort(403)
    require_owner()
    found = current_company()
    if found is None:
        abort(401)
    return found


@bp.post("/billing/checkout")
def checkout() -> ResponseReturnValue:
    """Choose a plan: form field choice = "starter:monthly" (billing.Choice)."""
    found = _owner_company()
    user = current_user()
    assert user is not None
    form = request.get_json(silent=True) if request.is_json else request.form
    choice = billing.parse_choice((form or {}).get("choice"))
    if choice is None:
        return _not_now("Choose one of the plans.")
    try:
        return _go(billing.checkout_url(found, user.email, *choice, back=_back()))
    except billing.BillingError as exc:
        return _not_now(str(exc))


@bp.post("/billing/portal")
def portal() -> ResponseReturnValue:
    """Manage billing: Stripe's customer portal."""
    found = _owner_company()
    try:
        return _go(billing.portal_url(found, back=_back()))
    except billing.BillingError as exc:
        return _not_now(str(exc))


@bp.get("/billing/return")
def came_back() -> ResponseReturnValue:
    """Where Stripe's Checkout sends the owner back to: Settings, saying what happened."""
    _only_in_signup_mode()
    if current_user() is None:
        abort(401)
    session["settings_note"] = ("Thank you: your plan is being set up. It shows here within a minute."
                                if request.args.get("done") else "No plan was chosen; nothing was charged.")
    return redirect("/#settings")


@bp.post("/billing/webhook")
def webhook() -> ResponseReturnValue:
    """Stripe's events (billing.EVENTS), signed with STRIPE_WEBHOOK_SECRET. Any other
    event gets 200 and is ignored; one already handled (by its id) too."""
    _only_in_signup_mode()
    secret = billing.webhook_secret()
    if not secret:
        log.error("A Stripe event came in, but STRIPE_WEBHOOK_SECRET isn't set")
        return jsonify({"error": "Billing isn't set up."}), 503
    if (request.content_length or 0) > MAX_EVENT_BYTES:
        abort(413)
    payload = request.get_data(cache=False)
    if len(payload) > MAX_EVENT_BYTES:
        abort(413)
    if not billing.verify(payload, request.headers.get("Stripe-Signature", ""), secret):
        log.warning("A Stripe event with a wrong or old signature was refused")
        return jsonify({"error": "Bad signature."}), 400
    try:
        event = json.loads(payload)
    except ValueError:
        return jsonify({"error": "Not JSON."}), 400
    if not isinstance(event, dict):
        return jsonify({"error": "Not an event."}), 400
    kind, event_id = str(event.get("type") or ""), event.get("id")
    if kind not in billing.EVENTS:
        return jsonify({"received": True, "handled": False})
    if isinstance(event_id, str) and event_id and not account_store.first_stripe_event(event_id, kind):
        return jsonify({"received": True, "handled": False, "duplicate": True})
    try:
        billing.handle(event)
    except Exception:
        log.error("Handling the Stripe event %s (%s) failed", event_id, kind, exc_info=True)
        if isinstance(event_id, str) and event_id:
            account_store.forget_stripe_event(event_id)   # Stripe sends it again later
        return jsonify({"error": "Not handled; send it again."}), 500
    return jsonify({"received": True, "handled": True})
