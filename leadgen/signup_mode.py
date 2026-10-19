"""Sign-up mode: the self-serve product any company can sign up for.

LEADGEN_SIGNUP = 1 (or true / yes / on) switches it on. Off (the default) the site is
AARCO's own Lead Finder, exactly as before. Everything sign-up mode changes asks on():
the public landing page (web/landing.py), the Terms and Privacy written for many
companies (web/legal.py), Yelp left out (pipeline.SearchParams.resolved_keys: the
owner's own Yelp key is never used for other companies) and the map data's credit in
the downloads (export.py).
"""

import os

ENV = "LEADGEN_SIGNUP"


def on() -> bool:
    """True when the site runs in sign-up mode (LEADGEN_SIGNUP is 1 / true / yes / on).

    Read from the environment on every call (no database), so a restart with the
    variable changed, or a test that sets it, takes effect at once.
    """
    return os.environ.get(ENV, "").strip().lower() in ("1", "true", "yes", "on")
