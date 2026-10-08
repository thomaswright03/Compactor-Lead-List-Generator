"""The administrator's checks (Find leads, "For the site administrator"): Check sources
and Send a test alert. Each is a plain form POST that answers with a result page
(templates/admin_check.html) and a way back; the section must be unlocked first."""

import logging

from flask import Blueprint, render_template, request
from flask.typing import ResponseReturnValue

from .. import alerts, checks
from .auth import admin_open
from .common import same_origin

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
                               text="Unlock the administrator's section on the Find leads page first."), 403
    return None


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
