"""Logging in and out, and who may open the site at all."""

import hashlib
import hmac
import ipaddress
import os
import re
import time

from flask import Blueprint, abort, current_app, jsonify, redirect, render_template, request, session
from flask.typing import ResponseReturnValue

from .common import same_origin, state, wants_page

LOGIN_DAYS = 30                 # how long a login lasts on a device
LOGIN_TRIES = 10                # wrong passwords allowed per address ...
LOGIN_WINDOW = 15 * 60          # ... in this many seconds

bp = Blueprint("auth", __name__)

# Whom the login page tells people to ask for access or a forgotten password: the
# LEADGEN_SUPPORT_CONTACT setting (a name with a phone number or email, set on Render),
# or else this.
SUPPORT_DEFAULT = "Ask the person who gave you your login, or Wright AI Solutions."
_REACH = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"
                    r"|(?:\+?1[\s.-]?)?\(?\b\d{3}\)?[\s.-]?\d{3}[\s.-]\d{4}\b")


def support_contact() -> list[tuple[str, str]]:
    """The LEADGEN_SUPPORT_CONTACT text in (text, link) pieces, an email address linked
    with mailto: and a phone number with tel: (so a phone offers to call it); [] when
    the setting is empty."""
    text = " ".join(os.environ.get("LEADGEN_SUPPORT_CONTACT", "").split()).rstrip(".")
    pieces, at = [], 0
    for found in _REACH.finditer(text):
        reach = found.group()
        if found.start() > at:
            pieces.append((text[at:found.start()], ""))
        href = (f"mailto:{reach}" if "@" in reach
                else "tel:" + ("+" if reach.startswith("+") else "") + re.sub(r"\D", "", reach))
        pieces.append((reach, href))
        at = found.end()
    if at < len(text):
        pieces.append((text[at:], ""))
    return pieces


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
    return render_template("login.html", error=error, username=name, contact=support_contact(),
                           no_contact=SUPPORT_DEFAULT), status


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


# The administrator's section (problems, site switches, the scoring check file) has its
# own password, ADMIN_PASSWORD, on top of the login. Unlocking lasts as long as the login.
def admin_token() -> str:
    """Changes when the admin password changes, which locks the section again everywhere."""
    key = current_app.secret_key
    key = key if isinstance(key, bytes) else str(key).encode()
    return hmac.new(key, f"admin\n{state().admin_password}".encode(), hashlib.sha256).hexdigest()[:32]


def admin_state() -> str:
    """"open", "locked", or "unset": a site with a login but no ADMIN_PASSWORD keeps the
    section locked for everyone. Without any login (a local copy) it is open."""
    s = state()
    if not s.admin_password:
        return "unset" if s.password else "open"
    return "open" if hmac.compare_digest(str(session.get("admin", "")), admin_token()) else "locked"


def admin_open() -> bool:
    return admin_state() == "open"


def admin_refusal() -> ResponseReturnValue:
    return jsonify({"error": "Unlock the administrator's section with its password first."}), 403


@bp.post("/admin/unlock")
def admin_unlock() -> ResponseReturnValue:
    s = state()
    if not same_origin():
        abort(403)
    if not s.admin_password:
        return jsonify({"error": "No administrator password is set up. Add ADMIN_PASSWORD "
                                 "in the server's settings." if s.password else ""}), 400
    now, who = time.time(), _client_address()
    recent = [t for t in s.admin_failures.get(who, []) if now - t < LOGIN_WINDOW]
    if len(recent) >= LOGIN_TRIES:
        return jsonify({"error": "Too many wrong tries. Wait 15 minutes and try again."}), 429
    supplied = str((request.get_json(silent=True) or {}).get("password") or "")
    if hmac.compare_digest(supplied.encode(), s.admin_password.encode()):
        s.admin_failures.pop(who, None)
        session["admin"] = admin_token()
        return jsonify({"admin": "open"})
    s.admin_failures[who] = [*recent, now]
    if len(s.admin_failures) > 5000:
        s.admin_failures.clear()
    time.sleep(0.5)
    return jsonify({"error": "Wrong password."}), 403    # not 401: that means logged out


@bp.post("/admin/lock")
def admin_lock() -> ResponseReturnValue:
    if not same_origin():
        abort(403)
    session.pop("admin", None)
    return jsonify({"admin": admin_state()})
