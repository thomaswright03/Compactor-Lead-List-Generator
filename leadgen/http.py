"""Small HTTP helper with retries and an on-disk response cache.

The cache means re-running the same search does not re-bill the Google API
and does not hammer the free OpenStreetMap servers.
"""

import hashlib
import json
import os
import re
import time
from pathlib import Path

import requests

from . import config

CACHE_DIR = Path(os.environ.get("LEADGEN_CACHE_DIR", ".cache"))


class HttpError(RuntimeError):
    pass


_SECRET_PARAM = re.compile(r"(?i)([?&](?:key|api_key)=)[^&\s)'\"]+")


def redact(text) -> str:
    """Hide API keys that request libraries echo back inside URLs."""
    return _SECRET_PARAM.sub(r"\1REDACTED", str(text))


def _cache_path(key: str) -> Path:
    return CACHE_DIR / (hashlib.sha256(key.encode()).hexdigest() + ".json")


def cache_get(key: str):
    path = _cache_path(key)
    try:
        if time.time() - path.stat().st_mtime > config.CACHE_TTL_SECONDS:
            return None
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def cache_put(key: str, value) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_path(key).write_text(json.dumps(value))
    except OSError:
        pass


def request_json(method, url, *, params=None, data=None, json_body=None, headers=None,
                 timeout=60, retries=3, use_cache=True, cache_key_extra="",
                 cacheable=None):
    """Return parsed JSON, retrying on network errors, 429 and 5xx.

    cacheable(value) -> bool can veto caching a response (e.g. a timeout notice).
    """
    key = json.dumps([method, url, params, data, json_body, cache_key_extra], sort_keys=True)
    if use_cache:
        cached = cache_get(key)
        if cached is not None:
            return cached

    hdrs = {"User-Agent": config.HTTP_USER_AGENT}
    hdrs.update(headers or {})
    last_error = None
    for attempt in range(retries):
        try:
            resp = requests.request(method, url, params=params, data=data, json=json_body,
                                    headers=hdrs, timeout=timeout)
            if resp.status_code == 429 or resp.status_code >= 500:
                last_error = HttpError(f"{url} returned HTTP {resp.status_code}")
            elif resp.status_code >= 400:
                raise HttpError(f"{url} returned HTTP {resp.status_code}: {redact(resp.text[:300])}")
            else:
                try:
                    value = resp.json()
                except ValueError:
                    # Overpass reports overload as an HTML page with status 200.
                    last_error = HttpError(f"{url} returned a non-JSON response")
                else:
                    if use_cache and (cacheable is None or cacheable(value)):
                        cache_put(key, value)
                    return value
        except requests.RequestException as exc:
            last_error = HttpError(f"{url}: {redact(exc)}")
        if attempt < retries - 1:
            time.sleep(2 ** (attempt + 1))
    raise last_error
