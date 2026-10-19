"""The public landing page of sign-up mode (signup_mode.on()): what Lead Finder is, how it
works, and the plans, with Start free trial (/signup) and Log in (/login).

At /welcome, open without logging in (accounts.PUBLIC). With sign-up mode off the page doesn't exist:
/welcome is handled like any other unknown address, so AARCO's site is unchanged.
"""

from typing import Any

from flask import Blueprint, abort, render_template

from .. import plans, signup_mode
from .auth import support_contact

bp = Blueprint("landing", __name__)

PITCH = ("See every business in your area likely to need what you service, scored and on a map. "
         "Mark who has it, log every call, and hide the customers you already have.")


def landing_context() -> dict[str, Any]:
    """What landing.html shows: the pitch, the plans (plans.PLANS, with the yearly price)
    and the trial's terms, and whom to ask for a custom plan."""
    return {
        "pitch": PITCH,
        "plans": [{**vars(plan), "yearly": plans.annual_price(plan)} for plan in plans.PLANS.values()],
        "trial_days": plans.TRIAL_DAYS, "free_months": plans.ANNUAL_FREE_MONTHS,
        "area_miles": plans.AREA_MAX_MILES, "contact": support_contact(),
    }


@bp.get("/welcome")
def welcome() -> str:
    """The landing page; 404 unless sign-up mode is on."""
    if not signup_mode.on():
        abort(404)
    return render_template("landing.html", **landing_context())

