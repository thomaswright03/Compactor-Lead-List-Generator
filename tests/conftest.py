import ipaddress
import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


def _local(address) -> bool:
    host = address[0] if isinstance(address, tuple) else address
    if not isinstance(host, str) or host == "localhost":
        return True                              # a Unix socket, or this machine
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True, scope="session")
def no_network(tmp_path_factory):
    """The tests never reach the internet (Google, Yelp, the map servers, ZIP lookups)
    and never write into the folder they are run from, even from a search thread that
    outlives its test: connections to anything but this machine fail, and the cache
    (with the SQLite database) is a temporary folder for the whole run."""
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6) and not _local(address):
            raise OSError(f"tests may not reach the network ({address[0]})")
        return real_connect(sock, address)

    def connect_ex(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6) and not _local(address):
            raise OSError(f"tests may not reach the network ({address[0]})")
        return real_connect_ex(sock, address)

    import leadgen.http
    real_cache = leadgen.http.CACHE_DIR
    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    leadgen.http.CACHE_DIR = tmp_path_factory.mktemp("session-cache")
    yield
    socket.socket.connect = real_connect
    socket.socket.connect_ex = real_connect_ex
    leadgen.http.CACHE_DIR = real_cache


@pytest.fixture(autouse=True)
def no_real_keys(monkeypatch, tmp_path):
    """Tests never see (or spend) real API keys, and never touch the real cache or
    daily usage counter."""
    for var in ("GOOGLE_PLACES_API_KEY", "YELP_API_KEY", "APP_PASSWORD", "DATABASE_URL", "RENDER",
                "LEADGEN_SEARCH_PAUSED", "LEADGEN_GOOGLE_OFF", "LEADGEN_YELP_OFF",
                "LEADGEN_SUPPORT_CONTACT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("leadgen.http.CACHE_DIR", tmp_path / "cache")
    # The map data's catch-up rounds don't wait for busy servers to cool down.
    monkeypatch.setattr("leadgen.config.OVERPASS_RETRY_PAUSE_SECONDS", 0)
    # Problem reports (alerts.py) only in the tests about them, and never to a real webhook.
    monkeypatch.delenv("LEADGEN_ALERT_WEBHOOK", raising=False)
    monkeypatch.setattr("leadgen.alerts.ON", False)
    monkeypatch.setattr("leadgen.alerts.BACKGROUND", False)
    monkeypatch.setattr("leadgen.alerts._last", {})
    # Tests use a fresh SQLite file; LEADGEN_TEST_DATABASE_URL runs them on a
    # throwaway Postgres instead (its tables are emptied before every test).
    test_db = os.environ.get("LEADGEN_TEST_DATABASE_URL")
    if test_db:
        monkeypatch.setenv("DATABASE_URL", test_db)
        from leadgen import store
        with store.connect() as db:
            for table in store.TABLES:
                db.run(f"DELETE FROM {table}")
