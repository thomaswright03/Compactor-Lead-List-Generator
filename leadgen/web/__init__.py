"""The website: find leads, work the saved list, log calls, see stats, download.

The routes live by page: auth.py (login); Find leads in finding.py (start and follow
today's search), place.py (where it will run), history.py (the search history) and
switch_routes.py (the emergency switches); leads.py (the saved list, marks, calls,
stats, downloads); mapping.py (the map); customer_routes.py (current customers, kept off
the lists); legal.py (Terms and Privacy); admin.py (the administrator's checks);
accounts.py (sign-up mode's accounts and Settings, signup_mode.py). The page's script and
styles are in leadgen/static/.
"""

import datetime as dt
import hashlib
import hmac
import logging
import os
import secrets
import time  # noqa: F401 - tests patch web.time.sleep
from typing import Any

from flask import Flask, jsonify, redirect, render_template, request, session
from flask.typing import ResponseReturnValue
from werkzeug.exceptions import HTTPException

from .. import alerts, awake, calls, company, config, health, localtime, marks, signup_mode, store
from ..localtime import date_time_text
from ..pipeline import SearchParams
from ..scoring import PARENT_FLAG
from . import accounts, admin, auth, customer_routes, finding, history, leads, legal, mapping, place, switch_routes
from .auth import LOGIN_DAYS, LOGIN_TRIES, LOGIN_WINDOW
from .common import (
    DB_DOWN,
    MARKS_DOWN,
    NOT_USED_UP,
    SEARCH_PAUSED,
    LoadError,
    State,
    lead_json,
    load_saved,
    state,
    switches,
    wants_page,
    yelp_quota,
)
from .finding import MAX_JOBS, SLOW_SECONDS, STEPS, _Progress, plain_details, plain_warning, skipped_steps

__all__ = ["DB_DOWN", "LOGIN_DAYS", "LOGIN_TRIES", "LOGIN_WINDOW", "MARKS_DOWN", "MAX_JOBS",
           "NOT_USED_UP", "SEARCH_PAUSED", "SLOW_SECONDS", "STEPS", "LoadError", "_Progress",
           "create_app", "lead_json", "load_saved", "plain_details", "plain_warning",
           "setup_logging", "skipped_steps", "switches", "yelp_quota"]

log = logging.getLogger(__name__)

ERROR_PAGES = {
    400: ("Something in that request wasn't right", "Go back and try again."),
    401: ("You're logged out", "Log in again to keep working."),
    403: ("That page isn't available", "It can't be opened from here."),
    404: ("Page not found", "The address may be mistyped, or the page has moved."),
    405: ("That page isn't available", "It can't be opened this way."),
    429: ("Too many tries", "Wait a few minutes and try again."),
    500: ("Something went wrong", "The problem has been logged. Try again in a minute."),
    503: ("Can't reach the saved data right now", "Nothing is lost. Try again in a minute."),
}
# The pages of the app, each at its "#" address (/#leads): opening /leads in the browser
# lands there (_register_pages page_address).
PAGES = ("find", "leads", "calls", "map", "stats", "customers")
# Sign-up mode's page as well (the company's Settings, accounts.py).
SIGNUP_PAGES = (*PAGES, "settings")
STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")


def setup_logging() -> None:
    """Log to stderr (Render shows it under Logs) unless the host set up logging."""
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _asset_version() -> str:
    """Changes whenever the page's script or styles change, so browsers fetch the new ones."""
    digest = hashlib.sha256()
    for folder, _, files in sorted(os.walk(STATIC)):      # vendor/leaflet too
        for name in sorted(files):
            with open(os.path.join(folder, name), "rb") as f:
                digest.update(f.read())
    return digest.hexdigest()[:10]


def _error_page(code: int, title: str | None = None, text: str | None = None) -> tuple[str, int]:
    default_title, default_text = ERROR_PAGES.get(code, ERROR_PAGES[500])
    return render_template("error.html", code=code, title=title or default_title,
                           text=text or default_text), code


def _register_pages(app: Flask) -> None:
    """The page itself, the health check and the error pages."""
    version = _asset_version()

    @app.context_processor
    def page_globals() -> dict[str, Any]:
        return {"year": localtime.now().year,
                "login_on": bool(app.extensions["leadgen"].password) or signup_mode.on(),
                "user": session.get("user", ""), "asset_version": version, **company.page_names()}

    _register_request_scope(app)
    _register_errors(app)

    @app.get("/healthz")
    def healthz() -> ResponseReturnValue:
        # 503 while the database doesn't answer, so Render (healthCheckPath in render.yaml)
        # and any uptime monitor see the outage; the body still lists every source.
        body = health_check()
        return jsonify(body), 200 if body["ok"] else 503

    @app.get("/")
    def index() -> str:
        return render_template("index.html", **index_context())


def _register_request_scope(app: Flask) -> None:
    # One database connection per request, shared by everything the request reads.
    @app.before_request
    def open_scope() -> None:
        store.begin_scope()

    @app.teardown_request
    def close_scope(exc: BaseException | None) -> None:
        store.end_scope()

    # A page's address typed or bookmarked without its "#" (/leads, /map) opens the app on
    # that page; the page's own data requests to the same addresses (/leads, /stats) still
    # get their data. After the login check, which runs first.
    @app.before_request
    def page_address() -> ResponseReturnValue | None:
        name = request.path.strip("/")
        pages = SIGNUP_PAGES if signup_mode.on() else PAGES
        return redirect(f"/#{name}") if name in pages and wants_page() else None


def _register_errors(app: Flask) -> None:
    @app.errorhandler(HTTPException)
    def http_error(exc: HTTPException) -> ResponseReturnValue:
        code = exc.code or 500
        if wants_page() or request.path.startswith("/download/"):
            text = exc.description if getattr(exc, "plain", False) else None
            return _error_page(code, text=text)
        title = ERROR_PAGES.get(code, ERROR_PAGES[500])[0]
        return jsonify({"error": title}), code

    @app.errorhandler(Exception)
    def unexpected_error(exc: Exception) -> ResponseReturnValue:
        log.exception("Unexpected error on %s %s", request.method, request.path)
        if wants_page() or request.path.startswith("/download/"):
            return _error_page(500)
        return jsonify({"error": "Something went wrong. Try again in a minute."}), 500


def health_check() -> dict[str, Any]:
    """What /healthz answers (public, no login: never a key, an address or saved data).

    ok: the site can do its work, that is the database (saved leads, marks, calls) answered
    just now; false, with HTTP 503, while it doesn't. An outside source that fails (Yelp,
    Google, the map data, the webhook) doesn't make it false: searches carry on without
    it, and its entry under sources says so. version: the deployed commit; up_since: when
    this start of the site began (Utah time; the same time all day means it never went to
    sleep, awake.py); scoring_version: the scoring rules' version (config.SCORING_VERSION);
    sources: each source's standing (health.status): set up or not, when it last
    answered, when it last failed and why, Yelp's calls in the last 24 hours.
    """
    sources = health.status()
    return {"ok": bool(sources["database"]["ok"]), "version": os.environ.get("RENDER_GIT_COMMIT", "")[:7],
            "up_since": date_time_text(awake.started_at), "scoring_version": config.SCORING_VERSION,
            "sources": sources}


def index_context() -> dict[str, Any]:
    google, yelp_key, _ = SearchParams().resolved_keys()
    own = company.current()
    parent = [own.parent_company] if own.parent_company else []
    return {
        "defaults": {"location": own.address or config.DEFAULT_LOCATION, "radius": own.radius,
                     "keywords": ", ".join(own.keywords), "min_score": config.DEFAULT_MIN_SCORE},
        "yelp": yelp_quota(), "outcomes": list(calls.OUTCOMES), "google_on": bool(google),
        "yelp_on": bool(yelp_key), "switches": switches(), "paused_text": SEARCH_PAUSED,
        "undo_seconds": marks.UNDO_SECONDS, "admin": auth.admin_state(),
        "admin_lockable": bool(state().admin_password),
        "flagged": [*own.competitors, own.name, *parent], "parent_flag": PARENT_FLAG if parent else "",
        "area_miles": own.area_miles,
        # Sign-up mode: the Settings page, the email check and the search allowance.
        "account": accounts.page_context() if signup_mode.on() else {"signup": False},
    }


def create_app(password: str | None = None, username: str | None = None,
               admin_password: str | None = None) -> Flask:
    """password (or the APP_PASSWORD env var) puts the whole site behind a login page.

    username (or APP_USERNAME) is the name to log in with; without one any name
    works. Always set a password when the page is reachable from the internet:
    every search can spend the Google or Yelp API key. admin_password (or
    ADMIN_PASSWORD) locks the administrator's section (auth.admin_open).
    """
    setup_logging()
    alerts.install()
    app = Flask("leadgen", static_folder=STATIC)
    password = password if password is not None else os.environ.get("APP_PASSWORD", "")
    username = (username if username is not None else os.environ.get("APP_USERNAME", "")).strip()
    # Logins are kept in a signed cookie. SECRET_KEY can be set; otherwise the key is
    # derived from the login itself, so changing the password logs everyone out.
    app.secret_key = os.environ.get("SECRET_KEY") or hmac.new(
        f"{username}\n{password}".encode(), b"lead-finder-session-v1", hashlib.sha256).digest()
    if signup_mode.on() and not os.environ.get("SECRET_KEY"):
        # Everyone has their own login, so the cookie's key must be secret: without
        # SECRET_KEY a random one, which logs everyone out whenever the site restarts.
        log.warning("Sign-up mode without SECRET_KEY: logins end whenever the site restarts. Set SECRET_KEY.")
        app.secret_key = secrets.token_bytes(32)
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                      SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER")),
                      PERMANENT_SESSION_LIFETIME=dt.timedelta(days=LOGIN_DAYS),
                      SEND_FILE_MAX_AGE_DEFAULT=7 * 24 * 3600)   # asset URLs carry a version
    admin_password = admin_password if admin_password is not None else os.environ.get("ADMIN_PASSWORD", "")
    if password and not auth.support_contact():
        # Said in the logs on every start while there is none.
        log.warning("No login contact: config.SUPPORT_CONTACT and LEADGEN_SUPPORT_CONTACT are both empty, so "
                    "the login page names no one to ask for access or a forgotten password.")
    app.extensions["leadgen"] = State(password, username, admin_password)
    for blueprint in (auth.bp, accounts.bp, finding.bp, place.bp, history.bp, switch_routes.bp, leads.bp,
                      mapping.bp, customer_routes.bp, legal.bp, admin.bp):
        app.register_blueprint(blueprint)
    _register_pages(app)
    return app
