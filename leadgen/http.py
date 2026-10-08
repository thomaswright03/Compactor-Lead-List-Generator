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
        self.final = False      # retrying is pointless (a 4xx, or a used-up quota)


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


# Which source a request's host belongs to, for the status /healthz shows (health.py):
# the paid sources name theirs (request_json's `source`); the free ones are known by host.
_SOURCE_HOSTS = {"api.yelp.com": "yelp", "places.googleapis.com": "google",
                 "maps.googleapis.com": "geocoding", "nominatim.openstreetmap.org": "geocoding",
                 "api.zippopotam.us": "geocoding"}


def source_of(url: str) -> str | None:
    """The source a request to `url` counts for ("yelp", "google", "osm", "geocoding"), or None."""
    host = urlsplit(url).netloc.lower()
    if host in _SOURCE_HOSTS:
        return _SOURCE_HOSTS[host]
    if any(host == urlsplit(e).netloc.lower() for e in config.OVERPASS_ENDPOINTS):
        return "osm"
    return None


def _note(source: str | None, ok: bool, reason: str = "") -> None:
    """Record how the source answered (health.py), never letting that break the request."""
    if source is None:
        return
    from . import health  # it reads the database, which needs this module
    health.record(source, ok, reason)


def request_json(method: str, url: str, *, params: dict[str, Any] | None = None,
                 data: dict[str, Any] | str | None = None, json_body: Any = None,
                 headers: dict[str, str] | None = None,
                 timeout: float | tuple[float, float] = 60, retries: int = 3,
                 use_cache: bool = True, cache_key_extra: str = "",
                 cacheable: Callable[[Any], bool] | None = None, no_retry: Sequence[str] = (),
                 response_headers: MutableMapping[str, str] | None = None,
                 before_retry: Callable[[], bool] | None = None,
                 cache_ttl: float | None = None, source: str | None = None) -> Any:
    """Return parsed JSON, retrying on network errors, 429 and 5xx.

    cache_ttl: how long a cached answer is reused (default config.CACHE_TTL_SECONDS).
    cacheable(value) -> bool can veto caching a response (e.g. a timeout notice).
    no_retry: error-body text that makes retrying pointless (e.g. a used-up daily quota).
    response_headers: a dict that receives the response's headers.
    before_retry() -> bool is asked before each retry; False gives up (e.g. no calls left).
    source: which source the call counts for in /healthz (default: by the URL's host).
    """
    key = json.dumps([method, url, params, data, json_body, cache_key_extra], sort_keys=True)
    if use_cache:
        cached = cache_get(key, cache_ttl)
        if cached is not None:
            return cached
    hdrs = {"User-Agent": config.HTTP_USER_AGENT}
    hdrs.update(headers or {})
    source = source or source_of(url)
    last_error = None
    for attempt in range(retries):
        value, last_error = _attempt(method, url, params, data, json_body, hdrs, timeout,
                                     no_retry, response_headers, source)
        if last_error is None:
            if use_cache and (cacheable is None or cacheable(value)):
                cache_put(key, value)
            return value
        if last_error.final:
            raise last_error
        if attempt < retries - 1 and not _wait_before_retry(attempt, last_error, before_retry):
            break
    raise last_error or HttpError(f"{_where(url)}: no attempt was made")


def _attempt(method: str, url: str, params: dict[str, Any] | None, data: dict[str, Any] | str | None,
             json_body: Any, hdrs: dict[str, str], timeout: float | tuple[float, float],
             no_retry: Sequence[str], response_headers: MutableMapping[str, str] | None,
             source: str | None) -> tuple[Any, HttpError | None]:
    """One request: (its parsed JSON, None), or (None, the error); error.final means
    retrying is pointless. Every failure is one line: where, the status, how long it
    took and a short reason. How the source answered is noted for /healthz."""
    where = _where(url)
    started = time.monotonic()
    error: HttpError | None
    try:
        resp = requests.request(method, url, params=params, data=data, json=json_body,
                                headers=hdrs, timeout=timeout)
    except requests.RequestException as exc:
        error = HttpError(f"{where} failed {_after(started)}: {reason(str(exc))}")
        _note(source, False, str(error))
        return None, error
    if response_headers is not None:
        response_headers.clear()
        response_headers.update(resp.headers)
    error = _response_error(resp, where, started, no_retry)
    if error is None:
        try:
            value = resp.json()
        except ValueError:
            # Overpass reports overload as an HTML page with status 200.
            error = HttpError(f"{where} returned a non-JSON response {_after(started)}: {reason(resp.text)}")
    if error is not None:
        _note(source, False, str(error))
        return None, error
    _note(source, True)
    return value, None


def _response_error(resp: requests.Response, where: str, started: float,
                    no_retry: Sequence[str]) -> HttpError | None:
    """The error a response's status means, or None for a good one. 429 and 5xx are
    retried unless the body says retrying is pointless; other 4xx are final."""
    if resp.status_code < 400:
        return None
    error = HttpError(f"{where} returned HTTP {resp.status_code} {_after(started)}: {reason(resp.text)}",
                      resp.status_code)
    if resp.status_code == 429 or resp.status_code >= 500:
        error.final = any(t in resp.text for t in no_retry)
    else:
        error.final = True
    return error


def _wait_before_retry(attempt: int, error: HttpError, before_retry: Callable[[], bool] | None) -> bool:
    """Wait out the backoff before retry number attempt + 1; False when before_retry
    says to give up instead."""
    if before_retry is not None and not before_retry():
        return False
    wait = 2 ** (attempt + 1)
    log.info("%s; trying again in %d s", error, wait)
    time.sleep(wait)
    return True
