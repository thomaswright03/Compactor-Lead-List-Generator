"""What every test runs inside: no internet, no real keys or files, and no background
thread left running.

- The network: connections to anything but this machine fail for the whole run, in
  the socket layer and in requests, and the proxy settings are taken out of the
  environment first, so nothing can go out through a proxy on this machine either.
- Files: the app's cache folder (which holds the SQLite database) is a temporary
  folder, set before the app is imported, and each test gets its own; nothing is
  written into the folder the tests are run from.
- Background threads: every thread the app starts is named "leadgen ..."
  (leadgen.THREAD_PREFIX): a search, its heartbeat, a map fill-in, a map query, an
  alert. After each test, while its stand-ins (patched sources, its own database)
  are still in place, the fill-ins it started are ended and every such thread is
  waited for; one that is still running is stopped the way the site stops it (Pause
  searching), and a thread that won't end fails the test. The run fails as well if
  any is still alive at the end.
"""

import ipaddress
import os
import socket
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Before the app is imported (leadgen.http reads LEADGEN_CACHE_DIR then): no proxy, and
# a cache folder outside the working copy for the whole run.
PROXY_VARS = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy")
for _var in PROXY_VARS:
    os.environ.pop(_var, None)
os.environ["LEADGEN_CACHE_DIR"] = tempfile.mkdtemp(prefix="leadgen-tests-")

import pytest  # noqa: E402
import requests  # noqa: E402

from leadgen import THREAD_PREFIX  # noqa: E402

# How long a test's background threads get to end on their own, and then once stopped.
FINISH_SECONDS = 5.0
STOP_SECONDS = 20.0


def _local(address) -> bool:
    host = address[0] if isinstance(address, tuple) else address
    if not isinstance(host, str) or host == "localhost":
        return True                              # a Unix socket, or this machine
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _block_the_network() -> None:
    """Connections to anything but this machine fail, from here to the end of the run
    (never undone: a thread can't outlive the block)."""
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_send = requests.adapters.HTTPAdapter.send

    def connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6) and not _local(address):
            raise OSError(f"tests may not reach the network ({address[0]})")
        return real_connect(sock, address)

    def connect_ex(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6) and not _local(address):
            raise OSError(f"tests may not reach the network ({address[0]})")
        return real_connect_ex(sock, address)

    def send(adapter, request, *args, **kwargs):
        # Checked before any proxy is picked, so a proxy on this machine can't carry it out.
        host = urlsplit(request.url).hostname or ""
        if not _local(host):
            raise requests.ConnectionError(f"tests may not reach the network ({host})")
        return real_send(adapter, request, *args, **kwargs)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    requests.adapters.HTTPAdapter.send = send


_block_the_network()


def workers() -> list[threading.Thread]:
    """The app's background threads still alive."""
    return [t for t in threading.enumerate() if t.name.startswith(THREAD_PREFIX) and t.is_alive()]


def _join(seconds: float) -> list[threading.Thread]:
    """Wait up to `seconds` for the app's background threads to end, ending every
    fill-in as soon as it starts (it would otherwise ask again for up to an hour);
    returns the ones still alive."""
    from leadgen import fillin
    deadline = time.monotonic() + seconds
    while True:
        for ending in list(fillin._ending.values()):
            ending.set()
        left = workers()
        if not left or time.monotonic() >= deadline:
            return left
        left[0].join(min(0.1, max(0.0, deadline - time.monotonic())))


def finish_workers(monkeypatch) -> None:
    """The background threads the test started have ended (see the module's doc). One
    still running a few seconds after the test is stopped, and fails the test: a test
    waits for the searches it starts."""
    left = _join(FINISH_SECONDS)
    if not left:
        return
    names = ", ".join(t.name for t in left)
    # Stopped the way the site stops a search: Pause searching (a running search and the
    # map data look at it every few seconds).
    monkeypatch.setenv("LEADGEN_SEARCH_PAUSED", "1")
    left = _join(STOP_SECONDS)
    pytest.fail(f"background threads still running after the test: {names}"
                + (f"; these didn't stop even when searching was paused: {', '.join(t.name for t in left)}"
                   if left else "; they were stopped by pausing searching")
                + ". A test that starts a search, a fill-in or an alert waits for it to end.",
                pytrace=False)


@pytest.fixture(autouse=True, scope="session")
def no_threads_left():
    """The run fails if any of the app's background threads is still alive at its end."""
    yield
    left = _join(STOP_SECONDS)
    if left:
        pytest.fail("background threads still running at the end of the test run: "
                    + ", ".join(t.name for t in left), pytrace=False)


@pytest.fixture(autouse=True)
def no_real_keys(monkeypatch, tmp_path):
    """Tests never see (or spend) real API keys, and never touch the real cache or
    daily usage counter."""
    for var in ("GOOGLE_PLACES_API_KEY", "YELP_API_KEY", "APP_PASSWORD", "DATABASE_URL", "RENDER",
                "LEADGEN_SEARCH_PAUSED", "LEADGEN_GOOGLE_OFF", "LEADGEN_YELP_OFF",
                "LEADGEN_SUPPORT_CONTACT", *PROXY_VARS):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("leadgen.http.CACHE_DIR", tmp_path / "cache")
    # The ZIP code the tests search around is found offline (the site looks the place up
    # before a search starts; the ZIP lookup service is never reached from the tests).
    import leadgen.geo
    monkeypatch.setitem(leadgen.geo._KNOWN, "84101", (40.7559, -111.8967, "Salt Lake City, UT 84101"))
    # The map data's catch-up rounds don't wait for busy servers to cool down.
    monkeypatch.setattr("leadgen.config.OVERPASS_RETRY_PAUSE_SECONDS", 0)
    # The map areas a search missed are filled in in the background only in the tests
    # about that (test_fill_in.py switches it on).
    monkeypatch.setattr("leadgen.fillin.ON", False)
    # Problem reports (alerts.py) only in the tests about them, and never to a real webhook.
    monkeypatch.delenv("LEADGEN_ALERT_WEBHOOK", raising=False)
    monkeypatch.setattr("leadgen.alerts.ON", False)
    monkeypatch.setattr("leadgen.alerts.BACKGROUND", False)
    monkeypatch.setattr("leadgen.alerts._last", {})
    # Each test's server process starts with no running search of its own (interrupted.py).
    monkeypatch.setattr("leadgen.interrupted._live", {})
    # Tests use a fresh SQLite file; LEADGEN_TEST_DATABASE_URL runs them on a
    # throwaway Postgres instead (its tables are emptied before every test).
    test_db = os.environ.get("LEADGEN_TEST_DATABASE_URL")
    if test_db:
        monkeypatch.setenv("DATABASE_URL", test_db)
        from leadgen import store
        with store.connect() as db:
            for table in store.TABLES:
                db.run(f"DELETE FROM {table}")
    yield
    # Before the test's stand-ins are put back (this runs before monkeypatch's undo).
    finish_workers(monkeypatch)
    # The connections a test's requests left in the pool go with its database file.
    from leadgen import store
    store.close_pool()
