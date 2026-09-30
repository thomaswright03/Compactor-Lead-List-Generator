"""The emergency switches, flipped from inside the site (Find leads > Site switches).

Each switch (pause searching, Google off, Yelp off) is on when its environment
variable is set (config.SEARCH_PAUSED_ENV ...: the backup, which needs a restart)
or when someone switched it on in the site. The site's switches live in the
database, so they take effect on the next request in every process, with no
restart; who flipped each one, and when, is kept with it.
"""

import logging
import os
import time
from typing import Any

from . import config, store

log = logging.getLogger(__name__)

# The switches, by their environment variable, and what the page calls them.
NAMES = {config.SEARCH_PAUSED_ENV: "Pause searching",
         config.GOOGLE_OFF_ENV: "Switch Google off",
         config.YELP_OFF_ENV: "Switch Yelp off"}
# The short keys the page and its requests use.
KEYS = {"search_paused": config.SEARCH_PAUSED_ENV, "google_off": config.GOOGLE_OFF_ENV,
        "yelp_off": config.YELP_OFF_ENV}


def env_on(name: str) -> bool:
    """True when the environment variable `name` is set to 1 / true / yes / on."""
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _site(name: str) -> tuple[bool, str, float | None]:
    """(on, who, when) for the site's own switch; off when the database can't be read."""
    on, who, at, _ = _read(name)
    return on, who, at


def _read(name: str) -> tuple[bool, str, float | None, bool]:
    """(on, who, when, read) for the site's own switch; read is False when the
    database was there but didn't answer (the switch then counts as off)."""
    try:
        with store.connect() as db:
            row = db.one("SELECT value, by_name, at FROM switches WHERE name = ?", (name,))
    except store.Unavailable:
        return False, "", None, True    # no database (the command line on Render): env only
    except Exception:
        log.warning("Reading the site's switches failed; only the environment's count", exc_info=True)
        return False, "", None, False
    return (bool(row[0]), row[1] or "", row[2], True) if row else (False, "", None, True)


def is_on(name: str) -> bool:
    """True when the switch is on, in the environment or in the site."""
    return env_on(name) or _site(name)[0]


def set_switch(name: str, on: bool, by: str = "") -> None:
    """Flip the site's own switch (the environment's, if set, stays on regardless).
    Raises store.Unavailable or a database error when it can't be saved."""
    if name not in NAMES:
        raise ValueError(f"unknown switch {name}")
    with store.connect() as db:
        db.run("INSERT INTO switches (name, value, by_name, at) VALUES (?, ?, ?, ?) "
               "ON CONFLICT (name) DO UPDATE SET value = excluded.value, "
               "by_name = excluded.by_name, at = excluded.at",
               (name, int(on), store.person(by), time.time()))
    log.warning("%s %s in the site%s", NAMES[name], "switched on" if on else "switched off",
                f" by {store.person(by)}" if by else "")


def state() -> dict[str, dict[str, Any]]:
    """Every switch for the page: on, whether the environment holds it on, who / when,
    and "unread" when the database didn't answer (the page then can't show or flip it)."""
    out = {}
    for key, name in KEYS.items():
        site_on, who, at, read = _read(name)
        out[key] = {"on": env_on(name) or site_on, "env": env_on(name), "site": site_on,
                    "by": who, "at": at, "label": NAMES[name], "unread": not read}
    return out
