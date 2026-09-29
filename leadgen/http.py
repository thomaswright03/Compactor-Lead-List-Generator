"""Small HTTP helper with retries and an on-disk response cache.

The cache means re-running the same search does not re-bill the Google API
and does not hammer the free OpenStreetMap servers.
"""

import hashlib
import json
import os
import re
import time
from collections.abc import Callable, MutableMapping, Sequence
from pathlib import Path
from typing import Any

import requests

from . import config

CACHE_DIR = Path(os.environ.get("LEADGEN_CACHE_DIR", ".cache"))


class HttpError(RuntimeError):
    pass


_SECRET_PARAM = re.compile(r"(?i)([?&](?:key|api_key)=)[^&\s)'\"]+")


def redact(text: object) -> str:
    """Hide API keys that request libraries echo back inside URLs."""
    return _SECRET_PARAM.sub(r"\1REDACTED", str(text))


def _cache_path(key: str) -> Path:
    return CACHE_DIR / (hashlib.sha256(key.encode()).hexdigest() + ".json")


def cache_get(key: str, ttl: float | None = None) -> Any:
    path = _cache_path(key)
    try:
        if time.time() - path.stat().st_mtime > (ttl or config.CACHE_TTL_SECONDS):
            path.unlink(missing_ok=True)      # expired data is deleted, not just ignored
            return None
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def cache_put(key: str, value: object) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_path(key).write_text(json.dumps(value))
    except OSError:
        pass


def request_json(method: str, url: str, *, params: dict[str, Any] | None = None,
                 data: dict[str, Any] | str | None = None, json_body: Any = None,
                 headers: dict[str, str] | None = None,
                 timeout: float | tuple[float, float] = 60, retries: int = 3,
                 use_cache: bool = True, cache_key_extra: str = "",
                 cacheable: Callable[[Any], bool] | None = None, no_retry: Sequence[str] = (),
                 response_headers: MutableMapping[str, str] | None = None,
                 before_retry: Callable[[], bool] | None = None) -> Any:
    """Return parsed JSON, retrying on network errors, 429 and 5xx.

    cacheable(value) -> bool can veto caching a response (e.g. a timeout notice).
    no_retry: error-body text that makes retrying pointless (e.g. a used-up daily quota).
    response_headers: a dict that receives the response's headers.
    before_retry() -> bool is asked before each retry; False gives up (e.g. no calls left).
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
            if response_headers is not None:
                response_headers.clear()
                response_headers.update(resp.headers)
            if resp.status_code == 429 or resp.status_code >= 500:
                last_error = HttpError(f"{url} returned HTTP {resp.status_code}: "
                                       f"{redact(resp.text[:300])}")
                if any(t in resp.text for t in no_retry):
                    raise last_error
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
            if before_retry is not None and not before_retry():
                break
            time.sleep(2 ** (attempt + 1))
    raise last_error or HttpError(f"{url}: no attempt was made")
