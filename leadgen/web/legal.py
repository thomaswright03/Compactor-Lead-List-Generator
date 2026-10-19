"""The Terms of use and Privacy policy pages, open without logging in.

AARCO's site shows the versions written for AARCO (terms.html, privacy.html). Sign-up
mode (signup_mode.on()) shows the versions written for the subscription service sold to
many companies (terms_signup.html, privacy_signup.html), which still need a lawyer's
review before real customers sign up (docs/developer-overview.md).
"""

from typing import Any

from flask import Blueprint, render_template

from .. import config, plans, signup_mode
from .auth import LOGIN_DAYS, support_contact

bp = Blueprint("legal", __name__)

# When the current wording took effect; change it with the wording.
EFFECTIVE = "October 19, 2026"
SIGNUP_EFFECTIVE = "October 19, 2026"     # the sign-up versions'


def _page(template: str, title: str) -> str:
    contact = "".join(text for text, _ in support_contact()) or "Wright AI Solutions"
    if signup_mode.on():
        signup: dict[str, Any] = {"company": "Wright AI Solutions", "site_name": "Lead Finder",
                                  "trial_days": plans.TRIAL_DAYS}
        return render_template(template.replace(".html", "_signup.html"), title=title,
                               effective=SIGNUP_EFFECTIVE, contact=contact, login_days=LOGIN_DAYS, **signup)
    return render_template(template, title=title, effective=EFFECTIVE, contact=contact,
                           login_days=LOGIN_DAYS, company=config.OWN_COMPANY)


@bp.get("/terms")
def terms() -> str:
    return _page("terms.html", "Terms of service" if signup_mode.on() else "Terms of use")


@bp.get("/privacy")
def privacy() -> str:
    return _page("privacy.html", "Privacy policy")
