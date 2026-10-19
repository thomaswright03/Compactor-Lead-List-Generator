"""Sign-up mode's billing through Stripe (billing.py, web/billing_routes.py): Choose a
plan and Manage billing ask Stripe with the right form fields (the HTTP call is a
stand-in: no network, never a real key), only the owner may, the webhook takes only
correctly signed, recent events and keeps the company's plan, status and trial end as
Stripe has them, and without sign-up mode none of it is there. On SQLite and (with
LEADGEN_TEST_DATABASE_URL) on Postgres."""

import dataclasses
import hashlib
import hmac
import html
import json
import time

import pytest

from leadgen import accounts, allowance, billing, web
from leadgen.company import Company

PASSWORD = "correct horse battery"
SECRET = "whsec_tests_only"
PRICES = {"STRIPE_PRICE_STARTER": "price_starter_m", "STRIPE_PRICE_TEAM": "price_team_m",
          "STRIPE_PRICE_STARTER_YEARLY": "price_starter_y", "STRIPE_PRICE_TEAM_YEARLY": "price_team_y"}


@pytest.fixture
def signup(monkeypatch):
    monkeypatch.setenv("LEADGEN_SIGNUP", "1")
    monkeypatch.setenv("SECRET_KEY", "tests-only-secret")
    monkeypatch.setattr(web.accounts, "send_email", lambda to, subject, body, link: True)


@pytest.fixture
def stripe(monkeypatch, signup):
    """Billing set up with stand-in keys; every call to Stripe is recorded and answered
    here: (method, url, form, headers)."""
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_not_real")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", SECRET)
    for name, value in PRICES.items():
        monkeypatch.setenv(name, value)
    sent: list[tuple[str, str, dict, dict]] = []

    def fake(method, url, *, data=None, headers=None, **kw):
        sent.append((method, url, dict(data or {}), dict(headers or {})))
        assert kw.get("use_cache") is False
        if url.endswith("/checkout/sessions"):
            return {"id": "cs_1", "url": "https://checkout.stripe.com/c/pay/cs_1"}
        return {"id": "bps_1", "url": "https://billing.stripe.com/p/session/bps_1"}
    monkeypatch.setattr(billing.http, "request_json", fake)
    return sent


@pytest.fixture
def app(signup):
    return web.create_app()


def _sign_up(client, email="owner@acme.example"):
    res = client.post("/signup", data={"company": "Acme Balers", "name": "Pat Owner", "email": email,
                                       "password": PASSWORD})
    assert res.status_code == 302, res.get_data(as_text=True)
    return accounts.user_by_email(email).company_id


def _member(app, company_id):
    accounts.join(company_id, "member@acme.example", "Sam Member", PASSWORD)
    client = app.test_client()
    assert client.post("/login", data={"email": "member@acme.example", "password": PASSWORD}).status_code == 302
    return client


def _page(client):
    return html.unescape(client.get("/", headers={"Accept": "text/html"}).get_data(as_text=True))


def _signed(payload: bytes, at: float | None = None, secret: str = SECRET) -> str:
    stamp = str(int(time.time() if at is None else at))
    sig = hmac.new(secret.encode(), stamp.encode() + b"." + payload, hashlib.sha256).hexdigest()
    return f"t={stamp},v1={sig}"


def _send(client, event: dict, header: str | None = None):
    payload = json.dumps(event).encode()
    return client.post("/billing/webhook", data=payload, content_type="application/json",
                       headers={"Stripe-Signature": header if header is not None else _signed(payload)})


_seq = iter(range(1, 10**6))


def _event(kind: str, obj: dict, event_id: str | None = None) -> dict:
    return {"id": event_id or f"evt_{next(_seq)}", "type": kind, "data": {"object": obj}}


def _subscription(cid, status="trialing", price="price_team_m", trial_end=None, sub_id="sub_1"):
    return {"id": sub_id, "object": "subscription", "customer": "cus_1", "status": status,
            "trial_end": trial_end, "metadata": {"company_id": cid},
            "items": {"data": [{"price": {"id": price}}]}}


# ---- off by default

def test_without_sign_up_mode_there_is_no_billing(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_not_real")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", SECRET)
    client = web.create_app().test_client()
    for path in ("/billing/checkout", "/billing/portal", "/admin/companies"):
        assert client.post(path, data={"choice": "starter:monthly"}).status_code == 404, path
    assert client.get("/billing/return").status_code == 404
    assert _send(client, _event("invoice.paid", {"customer": "cus_1"})).status_code == 404
    assert "Billing" not in _page(client)


# ---- the webhook's signature

def test_the_webhook_takes_only_a_correct_recent_signature(app, stripe):
    client = app.test_client()
    cid = _sign_up(client)
    other = app.test_client()                      # Stripe isn't logged in
    event = _event("customer.subscription.updated", _subscription(cid, status="active"))
    payload = json.dumps(event).encode()
    assert _send(other, event, header=_signed(payload, secret="whsec_wrong")).status_code == 400
    assert _send(other, event, header=_signed(payload, at=time.time() - 6 * 60)).status_code == 400
    assert _send(other, event, header="").status_code == 400
    assert _send(other, event, header="t=abc,v1=00").status_code == 400
    assert accounts.get_company(cid).status == "trialing"           # nothing changed
    assert _send(other, event, header=_signed(payload, at=time.time() - 4 * 60)).status_code == 200
    assert accounts.get_company(cid).status == "active"


def test_verify_checks_any_v1_signature_and_the_tolerance():
    payload, now = b'{"id": "evt"}', 1_800_000_000
    good = _signed(payload, at=now).split(",")[1]
    assert billing.verify(payload, f"t={now},v1=deadbeef,{good}", SECRET, now=now)
    assert not billing.verify(payload, f"t={now},{good}", SECRET, now=now + 301)
    assert not billing.verify(payload + b" ", f"t={now},{good}", SECRET, now=now)
    assert not billing.verify(payload, f"t={now},{good}", "", now=now)


def test_without_a_webhook_secret_events_are_refused(app, stripe, monkeypatch):
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET")
    res = _send(app.test_client(), _event("invoice.paid", {"customer": "cus_1"}))
    assert res.status_code == 503


# ---- each event updates the company

def test_the_events_keep_the_company_as_stripe_has_it(app, stripe):
    client = app.test_client()
    cid = _sign_up(client)
    hook = app.test_client()
    ends = time.time() + 10 * 86400
    res = _send(hook, _event("checkout.session.completed", {
        "id": "cs_1", "client_reference_id": cid, "customer": "cus_1", "subscription": "sub_1",
        "metadata": {"company_id": cid, "plan": "starter"}}))
    assert res.status_code == 200 and res.get_json()["handled"]
    found = accounts.get_company(cid)
    assert (found.stripe_customer_id, found.stripe_subscription_id, found.plan) == ("cus_1", "sub_1", "starter")

    _send(hook, _event("customer.subscription.created", _subscription(cid, trial_end=int(ends))))
    found = accounts.get_company(cid)
    assert found.status == "trialing" and found.plan == "team" and found.trial_ends_at == int(ends)

    _send(hook, _event("customer.subscription.updated",
                       _subscription(cid, status="active", price="price_starter_y")))
    found = accounts.get_company(cid)
    assert found.status == "active" and found.plan == "starter"

    _send(hook, _event("invoice.payment_failed", {"id": "in_1", "customer": "cus_1", "subscription": "sub_1"}))
    found = accounts.get_company(cid)
    assert found.status == "past_due" and "payment didn't go through" in allowance.refusal(found)

    _send(hook, _event("invoice.paid", {"id": "in_2", "customer": "cus_1", "amount_paid": 9900,
                                        "parent": {"subscription_details": {"subscription": "sub_1"}}}))
    assert accounts.get_company(cid).status == "active"

    _send(hook, _event("customer.subscription.updated", _subscription(cid, status="unpaid")))
    assert accounts.get_company(cid).status == "past_due"

    _send(hook, _event("customer.subscription.deleted", _subscription(cid, status="canceled")))
    found = accounts.get_company(cid)
    assert found.status == "canceled" and "plan is canceled" in allowance.refusal(found)
    # A late invoice.paid doesn't bring a canceled plan back.
    _send(hook, _event("invoice.paid", {"id": "in_3", "customer": "cus_1", "subscription": "sub_1",
                                        "amount_paid": 9900}))
    assert accounts.get_company(cid).status == "canceled"


def test_an_expired_first_payment_cancels_and_a_trial_invoice_changes_nothing(app, stripe):
    cid = _sign_up(app.test_client())
    hook = app.test_client()
    _send(hook, _event("invoice.paid", {"id": "in_0", "customer": "cus_1", "subscription": "sub_1",
                                        "amount_paid": 0, "metadata": {}}))
    assert accounts.get_company(cid).status == "trialing"           # no company has cus_1 yet, and $0
    _send(hook, _event("customer.subscription.updated", _subscription(cid, status="incomplete_expired")))
    assert accounts.get_company(cid).status == "canceled"


def test_an_old_subscription_ending_leaves_the_new_one(app, stripe):
    cid = _sign_up(app.test_client())
    hook = app.test_client()
    _send(hook, _event("customer.subscription.created", _subscription(cid, status="active", sub_id="sub_new")))
    _send(hook, _event("customer.subscription.deleted", _subscription(cid, status="canceled", sub_id="sub_old")))
    found = accounts.get_company(cid)
    assert found.status == "active" and found.stripe_subscription_id == "sub_new"


def test_events_are_handled_once_and_unknown_ones_ignored(app, stripe):
    cid = _sign_up(app.test_client())
    hook = app.test_client()
    failed = _event("invoice.payment_failed", {"customer": "cus_1", "subscription": "sub_1"}, "evt_same")
    _send(hook, _event("customer.subscription.created", _subscription(cid, status="active")))
    assert _send(hook, failed).get_json()["handled"]
    assert accounts.get_company(cid).status == "past_due"
    _send(hook, _event("customer.subscription.updated", _subscription(cid, status="active")))
    res = _send(hook, failed)                      # Stripe sends it again: no change
    assert res.status_code == 200 and res.get_json().get("duplicate")
    assert accounts.get_company(cid).status == "active"
    before = accounts.get_company(cid)
    res = _send(hook, _event("charge.refunded", {"id": "ch_1", "customer": "cus_1"}))
    assert res.status_code == 200 and res.get_json()["handled"] is False
    assert accounts.get_company(cid) == before


def test_an_event_for_no_company_is_taken_and_changes_nothing(app, stripe):
    cid = _sign_up(app.test_client())
    res = _send(app.test_client(), _event("customer.subscription.updated", {
        **_subscription("nosuchcompany", status="canceled"), "customer": "cus_other", "id": "sub_other"}))
    assert res.status_code == 200 and accounts.get_company(cid).status == "trialing"


# ---- Choose a plan and Manage billing

def test_choose_a_plan_opens_stripe_checkout_with_the_trial_left(app, stripe):
    client = app.test_client()
    cid = _sign_up(client)
    res = client.post("/billing/checkout", data={"choice": "team:yearly"})
    assert res.status_code == 303 and res.headers["Location"] == "https://checkout.stripe.com/c/pay/cs_1"
    method, url, form, headers = stripe[-1]
    assert (method, url) == ("POST", "https://api.stripe.com/v1/checkout/sessions")
    assert headers["Authorization"] == "Bearer sk_test_not_real" and headers["Idempotency-Key"]
    assert form["mode"] == "subscription"
    assert form["line_items[0][price]"] == "price_team_y" and form["line_items[0][quantity]"] == "1"
    assert form["client_reference_id"] == cid and form["metadata[company_id]"] == cid
    assert form["subscription_data[metadata][company_id]"] == cid
    assert form["subscription_data[trial_period_days]"] == "14"
    assert form["payment_method_collection"] == "always"
    assert form["customer_email"] == "owner@acme.example" and "customer" not in form
    assert form["success_url"] == "http://localhost/billing/return?done=1"
    assert form["cancel_url"] == "http://localhost/billing/return"
    # Once a customer, the same customer; JSON asked, the address answered.
    accounts.update_company(cid, stripe_customer_id="cus_1", trial_ends_at=time.time() + 3.5 * 86400)
    res = client.post("/billing/checkout", json={"choice": "starter:monthly"}, headers={"Accept": "application/json"})
    assert res.get_json() == {"url": "https://checkout.stripe.com/c/pay/cs_1"}
    form = stripe[-1][2]
    assert form["customer"] == "cus_1" and "customer_email" not in form
    assert form["line_items[0][price]"] == "price_starter_m" and form["subscription_data[trial_period_days]"] == "4"
    # After the trial: no trial days at all.
    accounts.update_company(cid, status="canceled")
    client.post("/billing/checkout", data={"choice": "starter:monthly"})
    assert not any(k.startswith("subscription_data[trial") for k in stripe[-1][2])


def test_back_from_checkout_settings_says_so(app, stripe):
    client = app.test_client()
    _sign_up(client)
    assert client.get("/billing/return?done=1").headers["Location"].endswith("/#settings")
    assert "your plan is being set up" in _page(client)
    client.get("/billing/return")
    assert "nothing was charged" in _page(client)


def test_manage_billing_opens_the_customer_portal(app, stripe):
    client = app.test_client()
    cid = _sign_up(client)
    res = client.post("/billing/portal")
    assert res.status_code == 303 and res.headers["Location"].endswith("/#settings") and stripe == []
    assert "Choose a plan first" in _page(client)
    accounts.update_company(cid, stripe_customer_id="cus_1", stripe_subscription_id="sub_1")
    page = _page(client)
    assert "Manage billing" in page and 'action="/billing/checkout"' not in page
    res = client.post("/billing/portal")
    assert res.status_code == 303 and res.headers["Location"] == "https://billing.stripe.com/p/session/bps_1"
    method, url, form, _ = stripe[-1]
    assert url == "https://api.stripe.com/v1/billing_portal/sessions"
    assert form == {"customer": "cus_1", "return_url": "http://localhost/#settings"}


def test_the_billing_card_offers_the_plans_set_up(app, stripe, monkeypatch):
    client = app.test_client()
    _sign_up(client)
    page = _page(client)
    assert 'id="billing"' in page and "Free trial" in page
    for value in ("starter:monthly", "starter:yearly", "team:monthly", "team:yearly"):
        assert f'value="{value}"' in page
    assert "Team, $249 a month" in page and "Starter, $990 a year" in page
    monkeypatch.delenv("STRIPE_PRICE_TEAM_YEARLY")
    assert 'value="team:yearly"' not in _page(client)


def test_only_the_owner_handles_billing(app, stripe):
    owner = app.test_client()
    cid = _sign_up(owner)
    member = _member(app, cid)
    assert member.post("/billing/checkout", data={"choice": "starter:monthly"}).status_code == 403
    assert member.post("/billing/portal").status_code == 403
    assert stripe == []
    page = _page(member)
    assert 'id="billing"' not in page and "Plan: Free trial" in page
    logged_out = app.test_client()
    assert logged_out.post("/billing/checkout", data={"choice": "starter:monthly"}).status_code == 401


def test_billing_refuses_other_sites(app, stripe):
    client = app.test_client()
    _sign_up(client)
    res = client.post("/billing/checkout", data={"choice": "starter:monthly"},
                      headers={"Origin": "https://evil.example"})
    assert res.status_code == 403 and stripe == []


def test_without_billing_set_up_the_page_says_so(app):
    client = app.test_client()
    _sign_up(client)
    page = _page(client)
    assert "Online payment isn't set up on this site yet" in page and 'action="/billing/checkout"' not in page
    res = client.post("/billing/checkout", data={"choice": "starter:monthly"})
    assert res.status_code == 303 and res.headers["Location"].endswith("/#settings")
    assert "Online payment isn't set up" in _page(client)
    assert client.post("/billing/portal").status_code == 303


def test_a_plan_without_its_price_or_a_wrong_choice_isnt_bought(app, stripe, monkeypatch):
    client = app.test_client()
    _sign_up(client)
    monkeypatch.delenv("STRIPE_PRICE_TEAM")
    res = client.post("/billing/checkout", data={"choice": "team:monthly"})
    assert res.status_code == 303 and res.headers["Location"].endswith("/#settings")
    client.post("/billing/checkout", data={"choice": "gold:monthly"})
    assert stripe == []


def test_stripe_down_is_a_plain_message(app, stripe, monkeypatch):
    client = app.test_client()
    _sign_up(client)

    def down(*args, **kwargs):
        raise billing.http.HttpError("api.stripe.com returned HTTP 500", 500)
    monkeypatch.setattr(billing.http, "request_json", down)
    res = client.post("/billing/checkout", data={"choice": "starter:monthly"})
    assert res.status_code == 303 and "couldn" in _page(client)


# ---- searching after the trial follows Stripe

def test_a_subscribed_trial_keeps_searching_while_stripe_charges(app):
    now = time.time()
    ended = Company(id="x" * 16, name="A", short_name="A", status="trialing", trial_ends_at=now - 3600)
    assert "free trial has ended" in allowance.can_search(ended)
    subscribed = dataclasses.replace(ended, stripe_subscription_id="sub_1")
    assert allowance.can_search(subscribed) is None
    late = dataclasses.replace(subscribed, trial_ends_at=now - allowance.TRIAL_GRACE_SECONDS - 60)
    assert "free trial has ended" in allowance.can_search(late)


# ---- the administrator's list

def test_the_administrator_lists_the_companies(signup, monkeypatch):
    app = web.create_app(admin_password="open-sesame")
    client = app.test_client()
    cid = _sign_up(client)
    _member(app, cid)
    assert client.post("/admin/companies").status_code == 403         # locked
    client.post("/admin/unlock", json={"password": "open-sesame"})
    res = client.post("/admin/companies")
    text = res.get_data(as_text=True)
    assert res.status_code == 200 and "Acme Balers" in text and "trialing" in text
    assert "<td>2</td>" in text and "1 company has signed up" in text
