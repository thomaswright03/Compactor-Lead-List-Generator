"""Sign-up mode's billing, through Stripe (signup_mode.py): a company's owner chooses a
plan on Stripe's own Checkout page, changes it, updates the card or cancels on Stripe's
customer portal, and Stripe tells the site what happened through its webhook
(web/billing_routes.py), which keeps the company's plan, status and trial end current
(accounts.update_company). Searching stops while the status is past_due or canceled
(allowance.can_search).

Stripe takes and keeps the card: the site keeps only Stripe's ids for the company's
customer and subscription, never a card number (as templates/privacy_signup.html says).
Its REST API is called with plain form-encoded requests (http.request_json), with no
Stripe library.

Settings (environment variables): STRIPE_SECRET_KEY (the API key), STRIPE_WEBHOOK_SECRET
(signs the webhook's events), STRIPE_PRICE_STARTER and STRIPE_PRICE_TEAM (each plan's
monthly price id, plans.PLANS) and, for yearly billing, STRIPE_PRICE_STARTER_YEARLY and
STRIPE_PRICE_TEAM_YEARLY. A plan can be bought here only when the key and its price id
are set (configured()); otherwise the Settings page says so instead.
"""

import hashlib
import hmac
import logging
import math
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any

from . import accounts, http, plans
from .company import Company

log = logging.getLogger(__name__)

API = "https://api.stripe.com/v1"
SECRET_ENV = "STRIPE_SECRET_KEY"
WEBHOOK_ENV = "STRIPE_WEBHOOK_SECRET"
# How old a webhook's signature may be (Stripe's own default tolerance).
TOLERANCE_SECONDS = 5 * 60
# What the Settings page says when a plan can't be bought here.
NOT_SET_UP = ("Online payment isn't set up on this site yet, so plans can't be chosen here. "
              "Ask whoever runs the site.")
STRIPE_DOWN = "The payment service couldn't be reached just now. Nothing was charged. Try again in a minute."

# Stripe's subscription status -> the company's (accounts.STATUSES). An unpaid
# subscription stays the customer's to fix (past_due); one whose first payment never
# went through has ended (canceled); a paused one (a trial ended with no card) can't search.
# "incomplete" (the first payment waits on the customer) changes nothing yet.
STATUS_OF = {"trialing": "trialing", "active": "active", "past_due": "past_due", "unpaid": "past_due",
             "paused": "past_due", "canceled": "canceled", "incomplete_expired": "canceled"}
EVENTS = ("checkout.session.completed", "customer.subscription.created", "customer.subscription.updated",
          "customer.subscription.deleted", "invoice.payment_failed", "invoice.paid")


class BillingError(RuntimeError):
    """Stripe couldn't be asked; the message is for the page."""


def secret_key() -> str:
    return os.environ.get(SECRET_ENV, "").strip()


def webhook_secret() -> str:
    return os.environ.get(WEBHOOK_ENV, "").strip()


def _price_env(plan_key: str, yearly: bool) -> str:
    return f"STRIPE_PRICE_{plan_key.upper()}" + ("_YEARLY" if yearly else "")


def price_id(plan_key: str, yearly: bool = False) -> str:
    """The Stripe price id of a plan (plans.PLANS key), monthly or yearly; "" when unset."""
    if plan_key not in plans.PLANS:
        return ""
    return os.environ.get(_price_env(plan_key, yearly), "").strip()


def configured(plan_key: str | None = None, yearly: bool = False) -> bool:
    """True when billing works: the secret key is set and, for a plan, its price id."""
    if not secret_key():
        return False
    if plan_key is None:
        return any(price_id(k) or price_id(k, True) for k in plans.PLANS)
    return bool(price_id(plan_key, yearly))


def plan_of_price(price: str) -> str | None:
    """The plan key (plans.PLANS) a Stripe price id is for, or None."""
    if not price:
        return None
    for key in plans.PLANS:
        if price in (price_id(key), price_id(key, True)):
            return key
    return None


@dataclass(frozen=True)
class Choice:
    """One way to buy a plan, as the Settings page offers it."""
    value: str                  # "starter:monthly"
    label: str                  # "Starter, $99 a month"


def choices() -> list[Choice]:
    """The plans and periods that can be bought here now (configured)."""
    out = []
    for key, plan in plans.PLANS.items():
        if configured(key):
            out.append(Choice(f"{key}:monthly", f"{plan.name}, ${plan.monthly_price} a month"))
        if configured(key, yearly=True):
            out.append(Choice(f"{key}:yearly", f"{plan.name}, ${plans.annual_price(plan):,} a year "
                              f"({plans.ANNUAL_FREE_MONTHS} months free)"))
    return out


def parse_choice(value: object) -> tuple[str, bool] | None:
    """("starter", yearly?) from a Choice's value, or None for anything else."""
    key, _, period = str(value or "").partition(":")
    if key not in plans.PLANS or period not in ("monthly", "yearly"):
        return None
    return key, period == "yearly"


# ---- asking Stripe

def _form(params: dict[str, Any], prefix: str = "") -> dict[str, str]:
    """Stripe's form encoding: nested dicts and lists as a[b][0]=c; None left out."""
    out: dict[str, str] = {}
    for name, value in params.items():
        key = f"{prefix}[{name}]" if prefix else str(name)
        if value is None:
            continue
        if isinstance(value, dict):
            out.update(_form(value, key))
        elif isinstance(value, list):
            out.update(_form({str(i): v for i, v in enumerate(value)}, key))
        elif isinstance(value, bool):
            out[key] = "true" if value else "false"
        else:
            out[key] = str(value)
    return out


def call(path: str, params: dict[str, Any]) -> dict[str, Any]:
    """POST to Stripe's API (form-encoded); its JSON answer. Retried on network errors
    and 5xx with the same idempotency key, so a retry never makes a second session.
    Raises BillingError (with a plain message) when it fails."""
    key = secret_key()
    if not key:
        raise BillingError(NOT_SET_UP)
    try:
        answer = http.request_json("POST", f"{API}{path}", data=_form(params), use_cache=False, retries=2,
                                   timeout=(10, 30),
                                   headers={"Authorization": f"Bearer {key}",
                                            "Idempotency-Key": uuid.uuid4().hex})
    except http.HttpError as exc:
        log.error("Stripe %s failed: %s", path, exc)
        raise BillingError(STRIPE_DOWN) from None
    if not isinstance(answer, dict):
        raise BillingError(STRIPE_DOWN)
    return answer


def trial_days_left(found: Company, now: float | None = None) -> int:
    """Whole days left of the company's free trial (rounded up), 0 once it ended or
    when it isn't on one."""
    if found.status != "trialing" or found.trial_ends_at is None:
        return 0
    left = found.trial_ends_at - (time.time() if now is None else now)
    return max(0, math.ceil(left / 86400))


def checkout_url(found: Company, email: str, plan_key: str, yearly: bool, back: str) -> str:
    """A Stripe Checkout page where the owner subscribes the company to the plan: the
    rest of its free trial still free (Stripe's trial), the card taken now
    (plans.CARD_REQUIRED). back: the site's address to come back to."""
    price = price_id(plan_key, yearly)
    if not secret_key() or not price:
        raise BillingError(NOT_SET_UP)
    days = trial_days_left(found)
    params: dict[str, Any] = {
        "mode": "subscription",
        "line_items": [{"price": price, "quantity": 1}],
        "client_reference_id": found.id,
        "metadata": {"company_id": found.id, "plan": plan_key},
        "subscription_data": {"metadata": {"company_id": found.id, "plan": plan_key},
                              "trial_period_days": days or None},
        "payment_method_collection": "always" if plans.CARD_REQUIRED else "if_required",
        "success_url": f"{back}/billing/return?done=1",
        "cancel_url": f"{back}/billing/return",
    }
    if found.stripe_customer_id:
        params["customer"] = found.stripe_customer_id
    else:
        params["customer_email"] = email
    url = call("/checkout/sessions", params).get("url")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise BillingError(STRIPE_DOWN)
    return url


def portal_url(found: Company, back: str) -> str:
    """Stripe's customer portal for the company: change the plan, update the card, see
    the invoices, cancel."""
    if not secret_key():
        raise BillingError(NOT_SET_UP)
    if not found.stripe_customer_id:
        raise BillingError("Choose a plan first; then you can manage your billing here.")
    url = call("/billing_portal/sessions", {"customer": found.stripe_customer_id,
                                            "return_url": f"{back}/#settings"}).get("url")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise BillingError(STRIPE_DOWN)
    return url


# ---- the webhook

def verify(payload: bytes, header: str, secret: str, now: float | None = None) -> bool:
    """True when the Stripe-Signature header ("t=...,v1=...") signs this payload with the
    webhook secret (HMAC-SHA256 of "t.payload"), made at most TOLERANCE_SECONDS ago."""
    if not secret or not header:
        return False
    stamp, signatures = "", []
    for part in header.split(","):
        name, _, value = part.strip().partition("=")
        if name == "t":
            stamp = value
        elif name == "v1":
            signatures.append(value)
    try:
        made = int(stamp)
    except ValueError:
        return False
    if abs((time.time() if now is None else now) - made) > TOLERANCE_SECONDS or not signatures:
        return False
    expected = hmac.new(secret.encode(), stamp.encode() + b"." + payload, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, s) for s in signatures)


def _id(value: object) -> str | None:
    """A Stripe id, whether the field holds the id or the expanded object."""
    if isinstance(value, dict):
        value = value.get("id")
    return value if isinstance(value, str) and value else None


def _metadata(obj: dict[str, Any]) -> dict[str, Any]:
    metadata = obj.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _company_for(obj: dict[str, Any], customer: str | None, subscription: str | None) -> Company | None:
    """The company an event is about: the id the site put on it (metadata, the Checkout's
    client_reference_id), else the Stripe ids it has on file."""
    metadata = _metadata(obj)
    for cid in (metadata.get("company_id"), obj.get("client_reference_id")):
        if isinstance(cid, str) and cid:
            found = accounts.get_company(cid)
            if found is not None:
                return found
    return accounts.company_by_stripe(customer, subscription)


def _subscription_price(sub: dict[str, Any]) -> str:
    items = sub.get("items")
    data = items.get("data") if isinstance(items, dict) else None
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return _id(data[0].get("price")) or ""
    return ""


def _checkout_completed(obj: dict[str, Any]) -> None:
    customer, subscription = _id(obj.get("customer")), _id(obj.get("subscription"))
    found = _company_for(obj, customer, subscription)
    if found is None:
        log.warning("Stripe checkout %s matches no company", obj.get("id"))
        return
    changes: dict[str, Any] = {}
    if customer:
        changes["stripe_customer_id"] = customer
    if subscription:
        changes["stripe_subscription_id"] = subscription
    plan_key = _metadata(obj).get("plan")
    if isinstance(plan_key, str) and plan_key in plans.PLANS:
        changes["plan"] = plan_key
    if changes:
        accounts.update_company(found.id, **changes)


def _subscription_changed(obj: dict[str, Any], deleted: bool) -> None:
    sub_id, customer = _id(obj.get("id")), _id(obj.get("customer"))
    found = _company_for(obj, customer, sub_id)
    if found is None or sub_id is None:
        log.warning("Stripe subscription %s matches no company", sub_id)
        return
    status = "canceled" if deleted else STATUS_OF.get(str(obj.get("status")))
    if found.stripe_subscription_id and found.stripe_subscription_id != sub_id and status in (None, "canceled"):
        return          # an old subscription ending, or a new one not yet paid: the current one stands
    changes: dict[str, Any] = {"stripe_subscription_id": sub_id}
    if customer:
        changes["stripe_customer_id"] = customer
    if status:
        changes["status"] = status
    plan_key = plan_of_price(_subscription_price(obj))
    if plan_key:
        changes["plan"] = plan_key
    trial_end = obj.get("trial_end")
    if status == "trialing" and isinstance(trial_end, (int, float)) and not isinstance(trial_end, bool):
        changes["trial_ends_at"] = float(trial_end)
    accounts.update_company(found.id, **changes)


def _invoice(obj: dict[str, Any], paid: bool) -> None:
    subscription = _id(obj.get("subscription"))
    parent = obj.get("parent")
    if subscription is None and isinstance(parent, dict):       # newer API versions
        details = parent.get("subscription_details")
        subscription = _id(details.get("subscription")) if isinstance(details, dict) else None
    found = _company_for({}, _id(obj.get("customer")), subscription)
    if found is None:
        log.warning("Stripe invoice %s matches no company", obj.get("id"))
        return
    if subscription and found.stripe_subscription_id and subscription != found.stripe_subscription_id:
        return          # an invoice of an older subscription
    if not paid:
        if found.status != "canceled":
            accounts.update_company(found.id, status="past_due")
        return
    # A paid invoice for money (not a trial's $0 one) brings an overdue or trialing
    # company back to active; a canceled one stays canceled.
    amount = obj.get("amount_paid")
    if isinstance(amount, int) and amount > 0 and found.status in ("past_due", "trialing"):
        accounts.update_company(found.id, status="active")


def handle(event: dict[str, Any]) -> bool:
    """Apply a verified webhook event to its company; True when it was one billing
    handles (EVENTS). Each sets the state an event says, so handling one twice changes
    nothing; the webhook also skips an event id it already handled."""
    kind = event.get("type")
    data = event.get("data")
    obj = data.get("object") if isinstance(data, dict) else None
    if kind not in EVENTS or not isinstance(obj, dict):
        return False
    if kind == "checkout.session.completed":
        _checkout_completed(obj)
    elif kind.startswith("customer.subscription."):
        _subscription_changed(obj, deleted=kind.endswith(".deleted"))
    else:
        _invoice(obj, paid=kind == "invoice.paid")
    return True
