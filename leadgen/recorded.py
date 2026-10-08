"""A local stand-in for Yelp and Google, for `python -m leadgen check-sources --recorded`.

It serves the recorded, sanitized answers in leadgen/data/recorded/ (made-up businesses
shaped like each API's real answers) on this machine, and the real client code
(sources/yelp.py, sources/google_places.py) is pointed at it: the same headers, paging,
parsing and retries as against the real APIs, with no key and nothing spent. Like the
real APIs it checks the key header, pages (Yelp by offset and limit, Google by a page
token), and it answers each source's first request with HTTP 503 once, so the retry
can be seen working. The run uses a throwaway database and cache folder, so nothing
is counted against the site's Yelp budget or noted in its status.
"""

import json
import os
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from . import THREAD_PREFIX, config, health, http, store
from .sources import google_places, yelp

FIXTURES = Path(__file__).resolve().parent / "data" / "recorded"
# Not real keys: what the stand-in expects in the Authorization / X-Goog-Api-Key header.
YELP_KEY = "recorded-yelp-key-not-a-real-key"
GOOGLE_KEY = "recorded-google-key-not-a-real-key"
GOOGLE_PAGE = 20
GOOGLE_TOKEN = "recorded-page-"


def _load(name: str, key: str) -> list[dict[str, Any]]:
    data = json.loads((FIXTURES / name).read_text())
    items: list[dict[str, Any]] = data[key]
    return items


class Server(ThreadingHTTPServer):
    """The stand-in: its recorded answers, and a count of the requests it answered."""

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.businesses = _load("yelp_search.json", "businesses")
        self.places = _load("google_search.json", "places")
        self.hiccups = {"yelp": 1, "google": 1}      # 503s still to answer, one per source
        self.requests: dict[str, int] = {"yelp": 0, "google": 0}
        self.lock = threading.Lock()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def count(self, source: str) -> None:
        with self.lock:
            self.requests[source] += 1

    def hiccup(self, source: str) -> bool:
        """True when this request is to be answered with a 503 (once per source)."""
        with self.lock:
            if self.hiccups[source]:
                self.hiccups[source] -= 1
                return True
            return False


class _Handler(BaseHTTPRequestHandler):
    server: Server

    def log_message(self, format: str, *args: Any) -> None:
        pass                                   # quiet: the table says what happened

    def _send(self, status: int, body: Any, headers: dict[str, str] | None = None) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        if parts.path != "/v3/businesses/search":
            self._send(404, {"error": {"code": "NOT_FOUND", "description": "Resource could not be found."}})
            return
        self.server.count("yelp")
        if self.headers.get("Authorization") != f"Bearer {YELP_KEY}":
            self._send(401, {"error": {"code": "TOKEN_INVALID", "description": "Invalid API key or access token."}})
            return
        if self.server.hiccup("yelp"):
            self._send(503, {"error": {"code": "INTERNAL_ERROR",
                                       "description": "Something went wrong internally, please try again later."}})
            return
        query = parse_qs(parts.query)
        offset = int(query.get("offset", ["0"])[0])
        limit = int(query.get("limit", [str(yelp.PAGE_SIZE)])[0])
        items = self.server.businesses
        self._send(200, {"businesses": items[offset:offset + limit], "total": len(items),
                         "region": {"center": {"latitude": config.OWN_COORDS[0],
                                               "longitude": config.OWN_COORDS[1]}}},
                   {"RateLimit-DailyLimit": "5000", "RateLimit-Remaining": str(5000 - self.server.requests["yelp"]),
                    "RateLimit-ResetTime": "2026-10-09T00:00:00+00:00"})

    def do_POST(self) -> None:
        if urlsplit(self.path).path != "/v1/places:searchText":
            self._send(404, {"error": {"code": 404, "message": "Not found", "status": "NOT_FOUND"}})
            return
        self.server.count("google")
        if self.headers.get("X-Goog-Api-Key") != GOOGLE_KEY:
            self._send(403, {"error": {"code": 403, "message": "The provided API key is invalid.",
                                       "status": "PERMISSION_DENIED",
                                       "details": [{"reason": "API_KEY_INVALID"}]}})
            return
        if not self.headers.get("X-Goog-FieldMask"):
            self._send(400, {"error": {"code": 400, "message": "A field mask is required.",
                                       "status": "INVALID_ARGUMENT"}})
            return
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.server.hiccup("google"):
            self._send(503, {"error": {"code": 503, "message": "The service is currently unavailable.",
                                       "status": "UNAVAILABLE"}})
            return
        token = str(body.get("pageToken") or "")
        page = int(token[len(GOOGLE_TOKEN):]) if token.startswith(GOOGLE_TOKEN) else 1
        start = (page - 1) * GOOGLE_PAGE
        answer: dict[str, Any] = {"places": self.server.places[start:start + GOOGLE_PAGE]}
        if start + GOOGLE_PAGE < len(self.server.places):
            answer["nextPageToken"] = f"{GOOGLE_TOKEN}{page + 1}"
        self._send(200, answer)


@contextmanager
def serving() -> Iterator[Server]:
    """The stand-in running on this machine, with the Yelp and Google client code
    pointed at it until the block ends."""
    server = Server()
    thread = threading.Thread(target=server.serve_forever, daemon=True, name=f"{THREAD_PREFIX}recorded stand-in")
    thread.start()
    urls = yelp.SEARCH_URL, google_places.SEARCH_URL
    yelp.SEARCH_URL = f"{server.url}/v3/businesses/search"
    google_places.SEARCH_URL = f"{server.url}/v1/places:searchText"
    try:
        yield server
    finally:
        yelp.SEARCH_URL, google_places.SEARCH_URL = urls
        server.shutdown()
        server.server_close()
        thread.join(5)


@contextmanager
def throwaway() -> Iterator[None]:
    """A temporary database and cache folder for the block, so the run counts and notes
    nothing of the site's (DATABASE_URL and RENDER are unset for it)."""
    kept = {name: os.environ.pop(name, None) for name in ("DATABASE_URL", "RENDER")}
    cache_dir = http.CACHE_DIR
    store.close_pool()
    health.forget()
    with tempfile.TemporaryDirectory(prefix="leadgen-recorded-") as tmp:
        http.CACHE_DIR = Path(tmp) / "cache"
        try:
            yield
        finally:
            store.close_pool()
            health.forget()
            http.CACHE_DIR = cache_dir
            for name, value in kept.items():
                if value is not None:
                    os.environ[name] = value


def _hiccup(server: Server, source: str) -> str:
    return "" if server.hiccups[source] else " (the first answered HTTP 503 and was retried)"


def run() -> dict[str, tuple[bool, float, str]]:
    """Run the real Yelp and Google searches against the stand-in: for each,
    (did it work, seconds, what happened)."""
    lat, lon = config.OWN_COORDS
    out: dict[str, tuple[bool, float, str]] = {}
    with throwaway(), serving() as server:
        started = time.monotonic()
        leads, _, warnings = yelp.search(lat, lon, 10.0, [next(iter(config.YELP_SEARCHES))], YELP_KEY,
                                         max_requests=10)
        phones = sum(bool(l.phone) for l in leads)
        expected = len(server.businesses)
        ok = len(leads) == expected and not warnings
        out["yelp"] = (ok, time.monotonic() - started,
                       f"{len(leads)} of {expected} recorded businesses parsed from {server.requests['yelp']} "
                       f"requests to the local stand-in{_hiccup(server, 'yelp')}; {phones} with a phone number"
                       + (f"; warnings: {' | '.join(warnings)}" if warnings else ""))
        started = time.monotonic()
        leads, _, warnings = google_places.search(lat, lon, 10.0, ["grocery store"], GOOGLE_KEY, max_requests=10)
        sites = sum(bool(l.website) for l in leads)
        expected = len(server.places)
        ok = len(leads) == expected and not warnings
        out["google"] = (ok, time.monotonic() - started,
                         f"{len(leads)} of {expected} recorded places parsed from {server.requests['google']} "
                         f"requests to the local stand-in{_hiccup(server, 'google')}; {sites} with a website"
                         + (f"; warnings: {' | '.join(warnings)}" if warnings else ""))
    return out
