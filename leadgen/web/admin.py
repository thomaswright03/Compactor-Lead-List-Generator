"""The administrator's page, /admin: open without an account login, locked with the
administrator password (ADMIN_PASSWORD) alone. Once unlocked it shows the recent
problems and the site switches, and has Check sources, Send a test alert, the scoring
check file and, in sign-up mode, the list of companies (their plan, status, trial end and
people). Plain forms throughout: each check answers with a result page
(templates/admin_check.html) and a way back."""

import hmac
import logging
import time

from flask import Blueprint, abort, redirect, render_template, request, session
from flask.typing import ResponseReturnValue

from .. import accounts, alerts, checks, localtime, signup_mode
from .. import switches as site_switches
from .auth import LOGIN_TRIES, LOGIN_WINDOW, admin_open, admin_state, admin_token, client_address
from .common import same_origin, state
from .switch_routes import _switch_list

bp = Blueprint("admin", __name__)
log = logging.getLogger("leadgen.web")


def _refused() -> ResponseReturnValue | None:
    """The error page a form sent from elsewhere or with the section locked gets (a
    form's POST is a page for the person, not a data request), or None to go on."""
    if not same_origin():
        return render_template("error.html", code=403, title="That page isn't available",
                               text="It can't be opened from here."), 403
    if not admin_open():
        return render_template("error.html", code=403, title="That page isn't available",
                               text="Unlock the administrator's page (/admin) with its password first."), 403
    return None


def _page(error: str = "", code: int = 200) -> ResponseReturnValue:
    opened = admin_state() == "open"
    problems, problems_error, switch_list = None, "", {}
    if opened:
        try:
            problems = alerts.recent()
        except Exception:
            log.warning("Loading the recent problems failed", exc_info=True)
            problems_error = "Couldn't load recent problems: the saved data can't be reached right now."
        switch_list = _switch_list()
    return render_template("admin.html", state=admin_state(), error=error, problems=problems,
                           problems_error=problems_error, switches=switch_list, signup=signup_mode.on(),
                           note=session.pop("admin_note", "")), code


@bp.get("/admin")
def page() -> ResponseReturnValue:
    """The administrator's page: the password box, or everything once unlocked."""
    return _page()


@bp.post("/admin")
def unlock() -> ResponseReturnValue:
    """The password box: right, and the page opens (for this browser, until it is locked
    again or the password changes); wrong, and it says so (with the login's limit on tries)."""
    if not same_origin():
        abort(403)
    s = state()
    if not s.admin_password:
        return _page("No administrator password is set up. Add ADMIN_PASSWORD in the server's settings.", 400)
    now, who = time.time(), client_address()
    recent = [t for t in s.admin_failures.get(who, []) if now - t < LOGIN_WINDOW]
    if len(recent) >= LOGIN_TRIES:
        return _page("Too many wrong tries. Wait 15 minutes and try again.", 429)
    if hmac.compare_digest((request.form.get("password") or "").encode(), s.admin_password.encode()):
        s.admin_failures.pop(who, None)
        session["admin"] = admin_token()
        return redirect("/admin")
    s.admin_failures[who] = [*recent, now]
    time.sleep(0.5)
    return _page("Wrong password.", 403)


@bp.post("/admin/relock")
def relock() -> ResponseReturnValue:
    if not same_origin():
        abort(403)
    session.pop("admin", None)
    return redirect("/admin")


@bp.post("/admin/switch")
def switch() -> ResponseReturnValue:
    """A site switch flipped from the page's form (it asks first in the browser)."""
    refused = _refused()
    if refused is not None:
        return refused
    name = site_switches.KEYS.get(request.form.get("key", ""))
    if name is None or request.form.get("on") not in ("1", "0"):
        abort(400)
    try:
        site_switches.set_switch(name, request.form["on"] == "1", "administrator")
        session["admin_note"] = "Saved. It works at once."
    except Exception:
        log.error("Saving a switch failed", exc_info=True)
        session["admin_note"] = "Not saved: the saved data can't be reached right now. Try again in a minute."
    return redirect("/admin")


@bp.post("/admin/check-sources")
def check_sources() -> ResponseReturnValue:
    """One cheapest real call per source (Yelp's counted against the daily limit)."""
    refused = _refused()
    if refused is not None:
        return refused
    rows = checks.run_all()
    bad = checks.failed(rows)
    return render_template("admin_check.html", title="Source check", rows=rows,
                           text=(f"{len(bad)} of the sources that are set up didn't answer." if bad
                                 else "Every source that is set up answered."))


@bp.post("/admin/test-alert")
def test_alert() -> ResponseReturnValue:
    """Send a test alert on every channel that is set up (email, the webhook) and say
    where it was delivered."""
    refused = _refused()
    if refused is not None:
        return refused
    try:
        sent = alerts.send_test(f"from the site by {request.form.get('by', '').strip()[:60]}"
                                if request.form.get("by", "").strip() else "from the site")
    except alerts.Unset as exc:
        return render_template("admin_check.html", title="Test alert", text=str(exc), ok=False)
    said = [f"Delivered {where}." for where in sent.delivered]
    if sent.failed:
        log.warning("The test alert could not be delivered on %d channel(s)", len(sent.failed))
        said += [f"The test alert couldn't be delivered {why}" for why in sent.failed]
    return render_template("admin_check.html", title="Test alert", ok=not sent.failed,
                           text=" ".join(said) + f" The message was: {sent.message}")


@bp.post("/admin/companies")
def companies() -> ResponseReturnValue:
    """Sign-up mode: every company that signed up, newest first, with its plan, status,
    trial end and how many people log in to it."""
    if not signup_mode.on():
        abort(404)
    refused = _refused()
    if refused is not None:
        return refused
    rows = [{"name": c.name, "plan": c.plan, "status": c.status, "users": users,
             "trial_ends": localtime.date_time_text(c.trial_ends_at) if c.trial_ends_at else "-",
             "since": localtime.date_time_text(c.created_at)}
            for c, users in accounts.all_companies()]
    return render_template("admin_check.html", title="Companies", companies=rows,
                           text=f"{len(rows)} {'company has' if len(rows) == 1 else 'companies have'} signed up.")
