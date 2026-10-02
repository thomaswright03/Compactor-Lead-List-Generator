"""Small HTTP helper with retries and an on-disk response cache.

The cache means re-running the same search does not re-bill the Google API
and does not hammer the free OpenStreetMap servers.
"""

import hashlib
import html
import json
import logging
import os
import re
import time
from collections.abc import Callable, MutableMapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests

from . import config

CACHE_DIR = Path(os.environ.get("LEADGEN_CACHE_DIR", ".cache"))
log = logging.getLogger(__name__)
# How much of a failed response's text goes into its error (and so into the log).
REASON_CHARS = 300


class HttpError(RuntimeError):
    """A request that failed; status is the HTTP status when the server answered."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


_SECRET_PARAM = re.compile(r"(?i)([?&](?:key|api_key)=)[^&\s)'\"]+")


def redact(text: object) -> str:
    """Hide API keys that request libraries echo back inside URLs."""
    return _SECRET_PARAM.sub(r"\1REDACTED", str(text))


_TITLE = re.compile(r"(?is)<(title|h1)\b[^>]*>(.*?)</\1\s*>")
_MARKUP = re.compile(r"(?is)<!--.*?-->|<(script|style)\b.*?</\1\s*>|<[!/?a-z][^>]*>")


def reason(text: str) -> str:
    """A failed response's text as one short line for the log and the page: an HTML
    error page ("<html><head><title>504 Gateway Time-out</title>...") becomes its title
    (else its words without the markup), a JSON error stays as it is, and whitespace and
    line breaks are collapsed. API keys are hidden."""
    title = _TITLE.search(text)
    if title and title.group(2).strip():
        text = title.group(2)
    if _MARKUP.search(text):
        text = _MARKUP.sub(" ", text)
    line = " ".join(html.unescape(redact(text)).split())
    return line if len(line) <= REASON_CHARS else line[:REASON_CHARS - 3].rstrip() + "..."


def _where(url: str) -> str:
    """'overpass-api.de/api/interpreter': the host and path, without the scheme or query."""
    parts = urlsplit(url)
    return (parts.netloc + parts.path) if parts.netloc else url


def _after(started: float) -> str:
    """'after 12.3s': how long the attempt that began at `started` (time.monotonic()) took."""
    return f"after {time.monotonic() - started:.1f}s"


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
    # Every failure is one line: where, the status, how long it took and a short reason.
    where = _where(url)
    last_error = None
    for attempt in range(retries):
        started = time.monotonic()
        try:
            resp = requests.request(method, url, params=params, data=data, json=json_body,
                                    headers=hdrs, timeout=timeout)
            if response_headers is not None:
                response_headers.clear()
                response_headers.update(resp.headers)
            if resp.status_code == 429 or resp.status_code >= 500:
                last_error = HttpError(f"{where} returned HTTP {resp.status_code} {_after(started)}: "
                                       f"{reason(resp.text)}", resp.status_code)
                if any(t in resp.text for t in no_retry):
                    raise last_error
            elif resp.status_code >= 400:
                raise HttpError(f"{where} returned HTTP {resp.status_code} {_after(started)}: {reason(resp.text)}",
                                resp.status_code)
            else:
                try:
                    value = resp.json()
                except ValueError:
                    # Overpass reports overload as an HTML page with status 200.
                    last_error = HttpError(f"{where} returned a non-JSON response {_after(started)}: "
                                           f"{reason(resp.text)}")
                else:
                    if use_cache and (cacheable is None or cacheable(value)):
                        cache_put(key, value)
                    return value
        except requests.RequestException as exc:
            last_error = HttpError(f"{where} failed {_after(started)}: {reason(str(exc))}")
        if attempt < retries - 1:
            if before_retry is not None and not before_retry():
                break
            wait = 2 ** (attempt + 1)
            log.info("%s; trying again in %d s", last_error, wait)
            time.sleep(wait)
    raise last_error or HttpError(f"{where}: no attempt was made")
