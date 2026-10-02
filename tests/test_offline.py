"""The tests themselves never reach the internet, write where they are run, or leave a
background thread running (tests/conftest.py)."""

import os
import socket
import threading
import time
from pathlib import Path

import conftest
import pytest
import requests

from leadgen import THREAD_PREFIX, config, web
from leadgen.pipeline import RunResult

ROOT = Path(__file__).resolve().parent.parent


def test_the_tests_cannot_reach_the_internet(tmp_path):
    with socket.socket() as sock, pytest.raises(OSError, match="may not reach the network"):
        sock.connect(("93.184.215.14", 443))
    import leadgen.http
    assert str(leadgen.http.CACHE_DIR) != ".cache"


def test_not_even_through_a_proxy_on_this_machine():
    assert not any(os.environ.get(var) for var in conftest.PROXY_VARS)
    with pytest.raises(requests.ConnectionError, match="may not reach the network"):
        requests.get("https://overpass-api.de/api/interpreter", timeout=1,
                     proxies={"https": "http://127.0.0.1:9", "http": "http://127.0.0.1:9"})


def test_the_default_cache_folder_is_outside_the_working_copy():
    default = Path(os.environ["LEADGEN_CACHE_DIR"]).resolve()
    assert ROOT not in default.parents and default != ROOT / ".cache"


def test_a_search_left_running_is_stopped_and_fails_its_test(monkeypatch):
    """A test that leaves a search running fails, and the search is stopped the way the
    site stops one (Pause searching) before the test's stand-ins are put back."""
    monkeypatch.setattr(conftest, "FINISH_SECONDS", 0.2)
    ended = []

    def run_until_paused(params, progress):
        while not config.stop_reason():
            time.sleep(0.02)
        ended.append(config.stop_reason())
        return RunResult([], (40.76, -111.89), "SLC", [], {})
    monkeypatch.setattr(web.finding, "run", run_until_paused)
    client = web.create_app().test_client()
    assert client.post("/search", data={"location": "84101"}).status_code == 200
    assert any(t.name.startswith(f"{THREAD_PREFIX}search ") for t in conftest.workers())
    with pytest.raises(pytest.fail.Exception, match="stopped by pausing searching"):
        conftest.finish_workers(monkeypatch)
    assert ended == ["the administrator paused searching"] and not conftest.workers()


def test_every_background_thread_is_named_for_the_check():
    """The check finds the app's threads by their name."""
    seen = threading.Event()
    thread = threading.Thread(target=seen.wait, args=(5,), name=f"{THREAD_PREFIX}example", daemon=True)
    thread.start()
    try:
        assert thread in conftest.workers()
    finally:
        seen.set()
        thread.join()
    assert thread not in conftest.workers()


def test_the_app_names_every_thread_it_starts():
    """Every thread and thread pool leadgen/ starts is named with THREAD_PREFIX, so the
    check after each test sees it."""
    import ast
    starts = 0
    for path in (ROOT / "leadgen").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            called = ast.unparse(node.func)
            if called not in ("threading.Thread", "ThreadPoolExecutor"):
                continue
            starts += 1
            name = next((k.value for k in node.keywords
                         if k.arg == ("name" if called == "threading.Thread" else "thread_name_prefix")), None)
            first = name.values[0] if isinstance(name, ast.JoinedStr) else None
            assert isinstance(first, ast.FormattedValue) and ast.unparse(first.value) == "THREAD_PREFIX", \
                f"{path.name}, line {node.lineno}: {called} without a name starting with THREAD_PREFIX"
    assert starts >= 6
