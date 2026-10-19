"""Sign-up mode: the Lead Finder as a product any company can sign up for.

LEADGEN_SIGNUP = 1 (or true / yes / on) switches it on. Off (the default) the site is
AARCO's own Lead Finder, with its one shared login, exactly as before. In sign-up mode
people sign up with their email and a password, each company gets its own saved leads,
marks, calls and searches (store.py keeps them apart), and the site's name and the
company's own details come from the company's settings (company.py) instead of AARCO's
(config.py). It also brings the public landing page (web/landing.py), the Terms and
Privacy written for many companies (web/legal.py), Yelp left out
(pipeline.SearchParams.resolved_keys: the owner's own Yelp key is never used for other
companies), the map data's credit in the downloads (export.py) and billing (billing.py).
"""

import os

ENV = "LEADGEN_SIGNUP"


def on() -> bool:
    """True when the site runs in sign-up mode (LEADGEN_SIGNUP is 1 / true / yes / on).

    Read from the environment on every call (no database), so a restart with the
    variable changed, or a test that sets it, takes effect at once.
    """
    return os.environ.get(ENV, "").strip().lower() in ("1", "true", "yes", "on")
