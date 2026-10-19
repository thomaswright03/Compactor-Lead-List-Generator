"""Sign-up mode: the Lead Finder as a product any company can sign up for.

With LEADGEN_SIGNUP=1 in the environment, people sign up with their email and a
password, each company gets its own saved leads, marks, calls and searches (store.py
keeps them apart), and the site's name and the company's own details come from the
company's settings (company.py) instead of AARCO's (config.py). Without it the site is
AARCO's own Lead Finder, with its one shared login, exactly as before.
"""

import os

ENV = "LEADGEN_SIGNUP"


def on() -> bool:
    """True when the site runs in sign-up mode (LEADGEN_SIGNUP=1)."""
    return os.environ.get(ENV, "").strip() == "1"
