"""Logging in and out, and who may open the site at all."""

import hashlib
import hmac
import ipaddress
import os
import time

from flask import Blueprint, abort, current_app, jsonify, redirect, render_template, request, session
from flask.typing import ResponseReturnValue

from .common import same_origin, state, wants_page

LOGIN_DAYS = 30                 # how long a login lasts on a device
LOGIN_TRIES = 10                # wrong passwords allowed per address ...
LOGIN_WINDOW = 15 * 60          # ... in this many seconds

bp = Blueprint("auth", __name__)


def _allowed_hosts() -> set[str]:
    return {"localhost"} | {h.strip().lower()
                            for h in os.environ.get("LEADGEN_ALLOWED_HOSTS", "").split(",")
                            if h.strip()}


def _host_allowed() -> bool:
    """Block DNS rebinding: a foreign site's name that now points at this server."""
    host = request.host.lower()
    name = host[1:host.index("]")] if host.startswith("[") else host.rsplit(":", 1)[0]
    try:
        ipaddress.ip_address(name)
        return True           # an IP literal cannot be rebound
    except ValueError:
        return name in _allowed_hosts()


def login_token() -> str:
    """Changes when the username or password changes, which ends existing logins."""
    key = current_app.secret_key
    key = key if isinstance(key, bytes) else str(key).encode()
    s = state()
    return hmac.new(key, f"{s.username}\n{s.password}".encode(), hashlib.sha256).hexdigest()[:32]


def _client_address() -> str:
    # On Render the last X-Forwarded-For entry is the one its proxy added (the visitor).
    forwarded = request.headers.get("X-Forwarded-For", "")
    if os.environ.get("RENDER") and forwarded:
        return forwarded.split(",")[-1].strip()
    return request.remote_addr or ""


@bp.before_app_request
def require_login() -> ResponseReturnValue | None:
    if request.path == "/healthz" or request.path.startswith("/static/"):
        return None           # hold no data; /healthz shows uptime checks the deployed version
    if not state().password:
        # Without a login only local/IP access is allowed (see LEADGEN_ALLOWED_HOSTS).
        return None if _host_allowed() else abort(403)
    if request.path == "/login":
        return None
    if session.get("login") == login_token():
        return None
    if request.method == "GET" and (wants_page() or request.path == "/"
                                    or request.path.startswith("/download/")):
        return redirect("/login")
    return jsonify({"error": "You were logged out. Reload the page to log in again."}), 401


def _login_page(error: str = "", name: str = "", status: int = 200) -> ResponseReturnValue:
    return render_template("login.html", error=error, username=name,
                           contact=os.environ.get("LEADGEN_SUPPORT_CONTACT", "").strip()
                           ), status


@bp.get("/login")
def login_page() -> ResponseReturnValue:
    if not state().password or session.get("login") == login_token():
        return redirect("/")
    return _login_page()


@bp.post("/login")
def login() -> ResponseReturnValue:
    s = state()
    if not s.password:
        return redirect("/")
    if not same_origin():
        abort(403)
    now, who = time.time(), _client_address()
    recent = [t for t in s.failures.get(who, []) if now - t < LOGIN_WINDOW]
    name = (request.form.get("username") or "").strip()
    if len(recent) >= LOGIN_TRIES:
        return _login_page("Too many wrong tries. Wait 15 minutes and try again.", name, 429)
    supplied = request.form.get("password") or ""
    name_ok = not s.username or hmac.compare_digest(name.encode(), s.username.encode())
    if name_ok and hmac.compare_digest(supplied.encode(), s.password.encode()):
        s.failures.pop(who, None)
        session.clear()
        session.permanent = True
        session.update(login=login_token(), user=s.username or name)
        return redirect("/")
    s.failures[who] = [*recent, now]
    if len(s.failures) > 5000:              # keep memory bounded
        s.failures.clear()
    time.sleep(0.5)
    return _login_page("Wrong username or password.", name, 401)


@bp.post("/logout")
def logout() -> ResponseReturnValue:
    if not same_origin():
        abort(403)
    session.clear()
    return redirect("/login")
