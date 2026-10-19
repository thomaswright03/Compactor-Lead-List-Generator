"""The Terms of use and Privacy policy pages, open without logging in."""

from flask import Blueprint, render_template

from .. import config
from .auth import LOGIN_DAYS, support_contact

bp = Blueprint("legal", __name__)

# When the current wording took effect; change it with the wording.
EFFECTIVE = "October 19, 2026"


def _page(template: str, title: str) -> str:
    contact = "".join(text for text, _ in support_contact()) or "Wright AI Solutions"
    return render_template(template, title=title, effective=EFFECTIVE, contact=contact,
                           login_days=LOGIN_DAYS, company=config.OWN_COMPANY)


@bp.get("/terms")
def terms() -> str:
    return _page("terms.html", "Terms of use")


@bp.get("/privacy")
def privacy() -> str:
    return _page("privacy.html", "Privacy policy")
