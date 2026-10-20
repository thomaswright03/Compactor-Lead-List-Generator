"""Sign-up mode's pages (signup_mode.py): signing up, logging in by email and password,
a forgotten password, the email check, joining a teammate's company, the setup after
signing up, and the company's Settings.

Each request of someone logged in works on their own company's data: require_account
(called by auth.require_login, first thing in every request) sets the company for the
request (store.set_company: its schema or file; company.use: its name, shop and area)
and close_account puts it back when the request ends. A search the request starts takes
the company with it (store.carry). Without sign-up mode none of these pages exist (404)
and the site keeps its one shared login (auth.py).
"""

import hashlib
import hmac
import logging
import time
from typing import Any

from flask import Blueprint, abort, current_app, g, jsonify, redirect, render_template, request, session
from flask.typing import ResponseReturnValue

from .. import accounts, allowance, billing, company, config, demo, localtime, mail, saved, signup_mode, store
from ..accounts import AccountError, User
from ..geo import GeocodeError, LookupDown, geocode
from .common import same_origin, state, wants_page
from .leads import forget_listing

log = logging.getLogger("leadgen.web")

bp = Blueprint("accounts", __name__)

# Open without logging in: the public pages and the account pages themselves.
# Stripe's webhook too (web/billing_routes.py: its signature is its proof).
PUBLIC = ("/healthz", "/terms", "/privacy", "/welcome", "/login", "/signup", "/forgot", "/logout",
          "/billing/webhook")
PUBLIC_STARTS = ("/static/", "/reset/", "/verify/", "/invite/")
# Sign-ups and password-reset emails allowed per address in SIGNUP_WINDOW seconds (wrong
# passwords use auth.LOGIN_TRIES, like the shared login).
SIGNUP_TRIES = 5
SIGNUP_WINDOW = 60 * 60
MAX_WORDS = 20
MAX_WORD_LEN = 60
MAX_COMPETITORS = 20


# ---- who is logged in, and their company for the request

def account_token(user: User) -> str:
    """Kept in the login cookie: changes when the password changes, which ends the user's
    other logins."""
    key = current_app.secret_key
    key = key if isinstance(key, bytes) else str(key).encode()
    return hmac.new(key, f"account\n{user.id}\n{user.password_hash}".encode(), hashlib.sha256).hexdigest()[:32]


def _logged_in() -> tuple[User, company.Company] | None:
    uid = session.get("uid")
    if not isinstance(uid, str) or not uid:
        return None
    try:
        user = accounts.user_by_id(uid)
        found = accounts.get_company(user.company_id) if user else None
    except Exception:
        log.error("Reading the account failed", exc_info=True)
        abort(503)
    if user is None or found is None or not hmac.compare_digest(str(session.get("login", "")),
                                                                 account_token(user)):
        return None
    return user, found


def enter(user: User, found: company.Company) -> None:
    """Work on this user's company for the rest of the request."""
    leave()
    g.account = (user, found)
    g.account_tokens = (store.set_company(found.id), company.use(found))


def leave() -> None:
    """Back to no company (the request ends, or enter() switches to new settings)."""
    tokens = g.pop("account_tokens", None)
    if tokens:
        store_token, company_token = tokens
        company.reset(company_token)
        store.reset_company(store_token)


def require_account() -> ResponseReturnValue | None:
    """auth.require_login in sign-up mode: someone logged in gets their company for the
    request; anyone else only the public pages (the landing page for /)."""
    who = _logged_in()
    if who is not None:
        enter(*who)
        return None
    if request.path in PUBLIC or request.path.startswith(PUBLIC_STARTS):
        return None
    if request.method == "GET" and request.path == "/":
        return redirect("/welcome")
    if request.method == "GET" and (wants_page() or request.path.startswith("/download/")):
        return redirect("/login")
    return jsonify({"error": "You were logged out. Reload the page to log in again."}), 401


@bp.teardown_app_request
def close_account(exc: BaseException | None) -> None:
    leave()


def current_user() -> User | None:
    account = g.get("account")
    return account[0] if account else None


def current_company() -> company.Company | None:
    account = g.get("account")
    return account[1] if account else None


def _log_in(user: User) -> None:
    session.clear()
    session.permanent = True
    session.update(uid=user.id, login=account_token(user), user=user.name)


# ---- the pages

def _only_in_signup_mode() -> None:
    if not signup_mode.on():
        abort(404)


def _page(mode: str, title: str, status: int = 200, **extra: Any) -> ResponseReturnValue:
    extra.setdefault("form", {})
    return render_template("account.html", mode=mode, title=title, min_password=accounts.MIN_PASSWORD,
                           **extra), status


def _address() -> str:
    from .auth import _client_address
    return _client_address()


def _too_many(kind: str) -> bool:
    """True when this address already did `kind` SIGNUP_TRIES times lately (and notes this try)."""
    tries = state().failures
    now, who = time.time(), f"{kind}:{_address()}"
    recent = [t for t in tries.get(who, []) if now - t < SIGNUP_WINDOW]
    if len(recent) >= SIGNUP_TRIES:
        return True
    tries[who] = [*recent, now]
    return False


def _link(path: str) -> str:
    return request.host_url.rstrip("/") + path


def send_email(to: str, subject: str, body: str, link: str) -> bool:
    """Email someone a link; without email set up (mail.py), the link goes to the log
    instead, so whoever runs the site can pass it on. True when it was emailed."""
    if not mail.set_up():
        log.warning("Email is not set up, so this link wasn't emailed to %s: %s", to, link)
        return False
    try:
        mail.send(subject, body, to=to)
        return True
    except RuntimeError as exc:
        log.warning("Emailing %s failed: %s; the link: %s", to, exc, link)
        return False


def send_verification(user: User) -> bool:
    token = accounts.make_token("verify", user.email, user_id=user.id, company_id=user.company_id)
    link = _link(f"/verify/{token}")
    return send_email(user.email, "Confirm your email for the Lead Finder",
                      f"Hello {user.name},\n\nTo confirm this is your email address, open this link:\n\n{link}\n\n"
                      "It works for 7 days. If you didn't sign up for the Lead Finder, you can ignore this email.",
                      link)


def login_page() -> ResponseReturnValue:
    """GET /login in sign-up mode (auth.login_page hands it over)."""
    from .auth import return_to
    then = return_to(request.args.get("next"))
    if _logged_in() is not None:
        return redirect(then or "/")
    return _page("login", "Log in", then=then)


def login() -> ResponseReturnValue:
    """POST /login in sign-up mode: email and password, the same limit on wrong tries as
    the shared login (auth.LOGIN_TRIES in auth.LOGIN_WINDOW per address)."""
    from .auth import LOGIN_TRIES, LOGIN_WINDOW, return_to
    if not same_origin():
        abort(403)
    s = state()
    now, who = time.time(), _address()
    recent = [t for t in s.failures.get(who, []) if now - t < LOGIN_WINDOW]
    form = {"email": (request.form.get("email") or "").strip()[:accounts.MAX_EMAIL]}
    then = return_to(request.form.get("next"))
    if len(recent) >= LOGIN_TRIES:
        return _page("login", "Log in", 429, form=form, then=then,
                     error="Too many wrong tries. Wait 15 minutes and try again.")
    if form["email"].lower() == demo.LOGIN:
        demo.ensure_quietly()           # the demo account is made the first time someone uses it
    user = accounts.check_login(form["email"], request.form.get("password") or "")
    if user is not None:
        s.failures.pop(who, None)
        _log_in(user)
        return redirect(then or "/")
    s.failures[who] = [*recent, now]
    if len(s.failures) > 5000:              # keep memory bounded
        s.failures.clear()
    time.sleep(0.5)
    return _page("login", "Log in", 401, form=form, then=then, error="Wrong email or password.")


@bp.route("/signup", methods=["GET", "POST"])
def signup() -> ResponseReturnValue:
    _only_in_signup_mode()
    if request.method == "GET":
        if _logged_in() is not None:
            return redirect("/")
        return _page("signup", "Sign up", purpose=f"Try the Lead Finder free for {accounts.TRIAL_DAYS} days: "
                     "it finds businesses near you likely to use a baler or compactor, and keeps your team's "
                     "answers and call notes in one list.")
    if not same_origin():
        abort(403)
    form = {k: (request.form.get(k) or "").strip()[:accounts.MAX_EMAIL] for k in ("company", "name", "email")}
    if _too_many("signup"):
        return _page("signup", "Sign up", 429, form=form, error="Too many sign-ups from here. Try again in an hour.")
    try:
        _, user = accounts.sign_up(form["company"], form["name"], form["email"], request.form.get("password") or "")
    except AccountError as exc:
        return _page("signup", "Sign up", 400, form=form, error=str(exc))
    _log_in(user)
    send_verification(user)
    return redirect("/setup")


@bp.route("/forgot", methods=["GET", "POST"])
def forgot() -> ResponseReturnValue:
    _only_in_signup_mode()
    if request.method == "GET":
        return _page("forgot", "Forgot your password?",
                     purpose="Enter your email, and we'll send you a link to choose a new password.")
    if not same_origin():
        abort(403)
    form = {"email": (request.form.get("email") or "").strip()[:accounts.MAX_EMAIL]}
    if _too_many("forgot"):
        return _page("forgot", "Forgot your password?", 429, form=form,
                     error="Too many tries from here. Try again in an hour.")
    try:
        user = accounts.user_by_email(accounts.email_of(form["email"]))
    except AccountError as exc:
        return _page("forgot", "Forgot your password?", 400, form=form, error=str(exc))
    if user is not None:
        token = accounts.make_token("reset", user.email, user_id=user.id)
        link = _link(f"/reset/{token}")
        send_email(user.email, "Choose a new Lead Finder password",
                   f"Hello {user.name},\n\nTo choose a new password, open this link:\n\n{link}\n\n"
                   "It works for 2 hours. If you didn't ask for it, you can ignore this email.", link)
    # The same answer either way, so the page doesn't tell who has an account.
    return _page("message", "Check your email", note="If there is an account for that email, we've sent it "
                 "a link to choose a new password. It works for 2 hours.", link="/login", link_text="Back to log in")


def _expired() -> ResponseReturnValue:
    return _page("message", "That link doesn't work any more", 404,
                 note="It has expired or was already used. Ask for a new one.", link="/login",
                 link_text="Go to log in")


@bp.route("/reset/<token>", methods=["GET", "POST"])
def reset(token: str) -> ResponseReturnValue:
    _only_in_signup_mode()
    found = accounts.peek_token(token, "reset")
    if found is None or not found.user_id:
        return _expired()
    if request.method == "GET":
        return _page("reset", "Choose a new password", action=f"/reset/{token}")
    if not same_origin():
        abort(403)
    password = request.form.get("password") or ""
    try:
        accounts.check_new_password(password)
    except AccountError as exc:
        return _page("reset", "Choose a new password", 400, action=f"/reset/{token}", error=str(exc))
    if accounts.use_token(token, "reset") is None:
        return _expired()
    accounts.set_password(found.user_id, password)
    accounts.mark_verified(found.user_id)        # the link reached their inbox
    user = accounts.user_by_id(found.user_id)
    if user is None:
        return _expired()
    _log_in(user)
    return redirect("/")


@bp.get("/verify/<token>")
def verify(token: str) -> ResponseReturnValue:
    _only_in_signup_mode()
    found = accounts.use_token(token, "verify")
    if found is None or not found.user_id:
        return _expired()
    accounts.mark_verified(found.user_id)
    return _page("message", "Your email is confirmed", note="Thank you. You can carry on finding leads.")


@bp.post("/verify/again")
def verify_again() -> ResponseReturnValue:
    _only_in_signup_mode()
    if not same_origin():
        abort(403)
    user = current_user()
    if user is None:
        abort(401)
    if not user.verified and not _too_many("verify"):
        sent = send_verification(user)
        session["settings_note"] = (f"We've sent a new link to {user.email}." if sent else
                                    "Email isn't set up on this site yet, so the link couldn't be sent.")
    return redirect("/")


@bp.route("/invite/<token>", methods=["GET", "POST"])
def invite(token: str) -> ResponseReturnValue:
    _only_in_signup_mode()
    found = accounts.peek_token(token, "invite")
    joining = accounts.get_company(found.company_id) if found and found.company_id else None
    if found is None or joining is None:
        return _expired()
    title = f"Join {joining.name}"
    if request.method == "GET":
        return _page("invite", title, company=joining.name, email=found.email, action=f"/invite/{token}")
    if not same_origin():
        abort(403)
    form = {"name": (request.form.get("name") or "").strip()[:accounts.MAX_NAME]}
    try:
        user = accounts.join(joining.id, found.email, form["name"], request.form.get("password") or "")
    except AccountError as exc:
        return _page("invite", title, 400, company=joining.name, email=found.email, action=f"/invite/{token}",
                     form=form, error=str(exc))
    accounts.use_token(token, "invite")
    _log_in(user)
    return redirect("/")


# ---- the company's settings: the setup after signing up, and Settings

def _words(text: str, most: int, longest: int, what: str) -> list[str]:
    words = list(dict.fromkeys(w for w in (" ".join(x.split()) for x in text.replace("\n", ",").split(",")) if w))
    if len(words) > most:
        raise AccountError(f"Use at most {most} {what} (you have {len(words)}).")
    long = next((w for w in words if len(w) > longest), None)
    if long:
        raise AccountError(f"Each of the {what} can be up to {longest} characters; “{long[:20]}…” is longer.")
    return words


def _radius(text: str) -> float:
    try:
        miles = float(text)
    except ValueError:
        raise AccountError("Your area must be a number of miles between 1 and 100.") from None
    if not 1 <= miles <= 100:
        raise AccountError("Your area must be a number of miles between 1 and 100.")
    return miles


def _changes(form: dict[str, str], now: company.Company) -> dict[str, Any]:
    """The company's details as the setup or Settings form gives them; raises AccountError."""
    changes: dict[str, Any] = {}
    if "name" in form:
        name = accounts.name_of(form["name"], "your company's name")
        changes.update(name=name, short_name=accounts.short_name(name))
    address = " ".join(form.get("address", "").split())[:200]
    if not address:
        raise AccountError("Enter your shop's address: a street address, city or ZIP code.")
    if address != now.address or not now.located:
        try:
            lat, lon, _ = geocode(address)
        except LookupDown:
            raise AccountError("The address couldn't be looked up just now. Try again in a minute.") from None
        except GeocodeError as exc:
            raise AccountError(str(exc)) from None
        changes.update(address=address, lat=round(lat, 6), lon=round(lon, 6))
    changes["radius_miles"] = _radius(form.get("radius", ""))
    changes["search_words"] = _words(form.get("search_words", ""), MAX_WORDS, MAX_WORD_LEN, "search words")
    changes["competitors"] = _words(form.get("competitors", ""), MAX_COMPETITORS, accounts.MAX_NAME, "competitors")
    return changes


def _save(form: dict[str, str]) -> company.Company:
    now = current_company()
    if now is None:
        abort(401)
    updated = accounts.update_company(now.id, **_changes(form, now))
    enter(g.account[0], updated)
    # The saved list is shown with distances from the shop and the company's own flags:
    # read it again with the new settings.
    saved.forget_parsed()
    forget_listing()
    return updated


def require_owner() -> User:
    user = current_user()
    if user is None:
        abort(401)
    if not user.owner:
        abort(403)
    return user


def _form_of(found: company.Company) -> dict[str, Any]:
    return {"name": found.name, "address": found.address, "radius": f"{found.radius_miles:g}",
            "search_words": ", ".join(found.search_words), "competitors": ", ".join(found.competitors)}


@bp.route("/setup", methods=["GET", "POST"])
def setup() -> ResponseReturnValue:
    _only_in_signup_mode()
    require_owner()
    found = current_company()
    assert found is not None
    standard = ", ".join(config.DEFAULT_KEYWORDS)
    title, purpose = "Set up your Lead Finder", "Tell us where your shop is and who your competitors are."
    if request.method == "GET":
        return _page("setup", title, purpose=purpose, company=found.name, form=_form_of(found),
                     standard_words=standard)
    if not same_origin():
        abort(403)
    form = {k: request.form.get(k) or "" for k in ("address", "radius", "search_words", "competitors")}
    try:
        _save(form)
    except AccountError as exc:
        return _page("setup", title, 400, purpose=purpose, company=found.name, form=form,
                     standard_words=standard, error=str(exc))
    return redirect("/#find")


@bp.post("/settings")
def settings() -> ResponseReturnValue:
    """The Settings page's form (the owner's): the company's name, shop, area, extra search
    words and competitors. Back to the page, with what happened."""
    _only_in_signup_mode()
    if not same_origin():
        abort(403)
    require_owner()
    form = {k: request.form.get(k) or "" for k in ("name", "address", "radius", "search_words", "competitors")}
    try:
        _save(form)
        session["settings_note"] = "Your settings are saved."
    except AccountError as exc:
        session["settings_note"] = f"Not saved: {exc}"
    return redirect("/#settings")


def seats_left(found: company.Company) -> int:
    """How many more people the company's plan lets log in (open invites count)."""
    users = allowance.plan_of(found).get("users", 1)
    taken = len(accounts.company_users(found.id)) + len(accounts.open_invites(found.id))
    return max(0, (users if isinstance(users, int) else 1) - taken)


@bp.post("/settings/invite")
def invite_teammate() -> ResponseReturnValue:
    """The owner invites a teammate by email: an invite link to set their password."""
    _only_in_signup_mode()
    if not same_origin():
        abort(403)
    owner = require_owner()
    found = current_company()
    assert found is not None
    try:
        email = accounts.email_of(request.form.get("email"))
        if accounts.user_by_email(email) is not None:
            raise AccountError(f"{email} already has a Lead Finder account.")
        if seats_left(found) <= 0:
            raise AccountError("Your plan has no room for another person.")
    except AccountError as exc:
        session["settings_note"] = f"Not invited: {exc}"
        return redirect("/#settings")
    if _too_many("invite"):
        session["settings_note"] = "Too many invites just now. Try again in an hour."
        return redirect("/#settings")
    token = accounts.make_token("invite", email, company_id=found.id)
    link = _link(f"/invite/{token}")
    sent = send_email(email, f"{owner.name} invited you to {found.name}'s Lead Finder",
                      f"Hello,\n\n{owner.name} invited you to {found.name}'s Lead Finder. To join, open this "
                      f"link and choose your password:\n\n{link}\n\nIt works for 7 days.", link)
    session["settings_note"] = (f"Invited {email}: we've emailed them a link." if sent else
                                f"Invited {email}. Email isn't set up on this site yet, so send them this link "
                                f"yourself: {link}")
    return redirect("/#settings")


def _day(at: float) -> str:
    day = localtime.utah(at)
    return f"{day:%B} {day.day}"


STATUS_TEXT = {"trialing": "Free trial", "active": "Active", "past_due": "Payment overdue: searches are paused",
               "canceled": "Canceled: searches are off"}


def billing_context(found: company.Company) -> dict[str, Any]:
    """The Settings page's Billing card (the owner's): the plan, its status and trial end,
    and what can be done here (billing.py)."""
    subscribed = bool(found.stripe_subscription_id) and found.status != "canceled"
    status, trial = STATUS_TEXT.get(found.status, found.status), ""
    if found.status == "trialing" and found.trial_ends_at:
        if subscribed:
            status = "Free trial; your card is charged for the plan when it ends"
            trial = f"The trial ends on {_day(found.trial_ends_at)}."
        elif found.trial_ends_at < time.time():
            status = "Free trial ended: searches are off until you choose a plan"
        else:
            trial = f"The trial ends on {_day(found.trial_ends_at)}; the rest of it stays free when you choose a plan."
    return {"status": status, "trial": trial, "configured": billing.configured(),
            "choices": [] if subscribed else billing.choices(),
            "manage": bool(found.stripe_customer_id) and bool(billing.secret_key()),
            "not_set_up": billing.NOT_SET_UP}


def page_context() -> dict[str, Any]:
    """What the main page shows in sign-up mode: the Settings page, the email check's
    banner and the search allowance (web/__init__.py index_context)."""
    user, found = current_user(), current_company()
    if user is None or found is None:
        return {"signup": False}
    left = allowance.allowance(found)
    refusal = allowance.can_search(found)
    return {
        "signup": True, "me": user, "owner": user.owner, "settings": _form_of(found),
        "located": found.located, "plan": allowance.plan_of(found).get("label", found.plan),
        "trial_ends": _day(found.trial_ends_at) if found.trial_ends_at else "",
        "team": accounts.company_users(found.id) if user.owner else [],
        "invited": accounts.open_invites(found.id) if user.owner else [],
        "seats_left": seats_left(found) if user.owner else 0,
        "settings_note": session.pop("settings_note", ""),
        "allowance": refusal or left.text(), "standard_words": ", ".join(config.DEFAULT_KEYWORDS),
        "billing": billing_context(found) if user.owner else {},
    }
