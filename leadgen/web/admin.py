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
    """Post a test message to the alert webhook and say whether it was delivered."""
    refused = _refused()
    if refused is not None:
        return refused
    try:
        sent = alerts.send_test(f"from the site by {request.form.get('by', '').strip()[:60]}"
                                if request.form.get("by", "").strip() else "from the site")
    except alerts.Unset as exc:
        return render_template("admin_check.html", title="Test alert", text=str(exc), ok=False)
    except Exception as exc:  # noqa: BLE001 - the page says what went wrong, by class and words
        log.warning("The test alert could not be delivered: %s", exc.__class__.__name__)
        return render_template("admin_check.html", title="Test alert", ok=False,
                               text=f"The test alert couldn't be delivered: {exc.__class__.__name__}: {exc}")
    return render_template("admin_check.html", title="Test alert", ok=True,
                           text=f"Delivered. The message was: {sent}")
