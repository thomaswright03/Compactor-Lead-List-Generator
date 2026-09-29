"""Whether a business has a baler, as marked on the results page ("yes" / "no").

Marks are kept per business, so they come back on later searches, in the same
store as the daily Yelp count (usage.py): Redis when REDIS_URL is set (on Render,
the Key Value store), otherwise .cache/baler-marks.json.
"""

import json
import re
import threading
import time

from . import http, usage

VALUES = ("yes", "no")
HASH_KEY = "leadgen:baler-marks"

_lock = threading.Lock()


class MarksUnavailable(RuntimeError):
    pass


def lead_key(lead):
    """A business's identity across searches: its name plus its phone number
    (or ZIP code, or rounded location, when it has no phone)."""
    name = re.sub(r"[^a-z0-9]+", "", (lead.name or "").lower())
    digits = re.sub(r"\D", "", lead.phone or "")[-10:]
    if len(digits) == 10:
        where = "tel" + digits
    elif lead.zip:
        where = "zip" + lead.zip[:5]
    else:
        where = f"at{round(lead.lat or 0, 3)},{round(lead.lon or 0, 3)}"
    return f"{name}|{where}"


def _file():
    return http.CACHE_DIR / "baler-marks.json"


def _read_file():
    try:
        data = json.loads(_file().read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _redis():
    r = usage.redis_client()
    if r is None and usage.on_render():
        raise MarksUnavailable("Baler marks can't be saved yet: the Key Value store is not "
                               "connected (REDIS_URL is not set; sync the Blueprint in Render)")
    return r


def get_all(keys):
    """{key: "yes"/"no"} for the given keys that have a mark."""
    keys = list(dict.fromkeys(keys))
    if not keys:
        return {}
    try:
        r = _redis()
    except MarksUnavailable:
        return {}
    if r is None:
        with _lock:
            data = _read_file()
        found = {k: data[k] for k in keys if k in data}
    else:
        try:
            found = dict(zip(keys, r.hmget(HASH_KEY, keys)))
        except Exception:
            return {}
    out = {}
    for k, raw in found.items():
        try:
            value = json.loads(raw)["v"] if isinstance(raw, str) else raw["v"]
        except (TypeError, ValueError, KeyError):
            continue
        if value in VALUES:
            out[k] = value
    return out


def set_mark(key, value, name=""):
    """Save a mark; an empty value clears it. Raises MarksUnavailable on failure."""
    if value not in VALUES + ("",):
        raise ValueError("A baler mark must be yes, no or empty")
    record = {"v": value, "name": name[:200], "at": int(time.time())}
    r = _redis()
    if r is None:
        with _lock:
            data = _read_file()
            if value:
                data[key] = record
            else:
                data.pop(key, None)
            try:
                http.CACHE_DIR.mkdir(parents=True, exist_ok=True)
                tmp = _file().with_suffix(".tmp")
                tmp.write_text(json.dumps(data))
                tmp.replace(_file())
            except OSError as exc:
                raise MarksUnavailable(f"Could not save the mark ({exc.__class__.__name__})")
        return
    try:
        if value:
            r.hset(HASH_KEY, key, json.dumps(record))
        else:
            r.hdel(HASH_KEY, key)
    except Exception as exc:
        raise MarksUnavailable(f"Could not save the mark ({exc.__class__.__name__})")


def apply(leads):
    """Set lead.has_baler from the saved marks."""
    marks = get_all(lead_key(l) for l in leads)
    for lead in leads:
        lead.has_baler = marks.get(lead_key(lead), "")
    return leads
