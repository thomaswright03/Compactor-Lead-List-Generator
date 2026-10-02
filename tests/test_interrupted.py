"""A server restart in the middle of a search (a deploy) keeps what the search had found,
saves it, and gives the day back at once, with a history line saying what happened."""

import threading
import time

from leadgen import config, daily, interrupted, saved, store, web
from leadgen.models import Lead
from leadgen.sources import collecting, osm, paging, report_found


def _lead(name, sid, lat=40.72, lon=-111.9, tags=("shop=wholesale",)):
    return Lead(name=name, lat=lat, lon=lon, source="osm", source_id=sid,
                raw_categories=list(tags))


FOUND = [_lead("Costco", "way/1"), _lead("Smith's Marketplace", "way/2", lat=40.73,
                                          tags=("shop=supermarket",))]


def _start(monkeypatch, found=FOUND):
    """Start a search that reports `found` and then waits (the server is about to stop).
    Returns (the old server's client, a function that ends the old search thread)."""
    gate, reported = threading.Event(), threading.Event()

    def run(params, progress):
        report_found(list(found))
        reported.set()
        gate.wait(10)
        raise RuntimeError("the old server's search thread ends with the test")
    monkeypatch.setattr(web.finding, "run", run)
    old = web.create_app().test_client()
    job = old.post("/search", data={"location": "84101", "radius": "30"}).get_json()["job_id"]
    assert reported.wait(5)

    def finish():
        gate.set()
        for _ in range(200):
            if old.get(f"/status/{job}").get_json()["state"] != "running":
                return
            time.sleep(0.05)
    return old, finish


def _new_server():
    """The process restarted: no search of its own runs, and a fresh app."""
    interrupted._live.clear()
    return web.create_app().test_client()


def test_a_deploy_mid_search_saves_what_it_found_and_gives_the_day_back(monkeypatch):
    _, finish = _start(monkeypatch)
    try:
        # The old server shuts down: its running search writes what it found and says so.
        interrupted._shutting_down()
        client = _new_server()
        body = client.get("/searches").get_json()
        assert not body["used_today"] and body["current"] is None and not body["cut_off"]
        [row] = body["searches"]
        assert row["interrupted"] and row["failed"] and row["leads"] == 2 and row["new"] == 2
        assert row["reason"].startswith("Interrupted by a server restart")
        assert "the 2 businesses it had found were saved" in row["reason"]
        assert row["place"] and row["details"]
        assert sorted(l.name for l in saved.load()) == ["Costco", "Smith's Marketplace"]
        # The day is free again straight away; the working rows are gone.
        assert daily.claim({"location": "84101"})[0] == daily.today()
        with store.connect() as db:
            assert db.all("SELECT id FROM search_runs") == []
            assert db.all("SELECT id FROM search_found") == []
    finally:
        finish()


def test_a_killed_server_is_finished_once_its_heartbeat_is_stale(monkeypatch):
    _, finish = _start(monkeypatch)
    try:
        [run] = interrupted._live.values()
        run.write()                       # a heartbeat with what was found so far
        run._stop.set()                   # then the process is killed: no goodbye
        client = _new_server()
        body = client.get("/searches").get_json()
        # Not stale yet: the page says the search was cut off and asks again shortly.
        assert body["cut_off"] and body["used_today"] and "leads" not in body["current"]
        assert saved.load() == []
        with store.connect() as db:
            db.run("UPDATE search_runs SET alive = alive - ?", (interrupted.STALE_SECONDS + 1,))
        body = client.get("/searches").get_json()
        assert not body["cut_off"] and not body["used_today"]
        assert body["searches"][0]["interrupted"] and body["searches"][0]["leads"] == 2
        assert len(saved.load()) == 2
        # Finished once only: another page finds nothing left to do.
        assert interrupted.recover() == 0
    finally:
        finish()


def test_a_restart_before_anything_was_found_frees_the_day_at_once(monkeypatch):
    _, finish = _start(monkeypatch, found=[])
    try:
        interrupted._shutting_down()
        client = _new_server()
        body = client.get("/searches").get_json()
        assert not body["used_today"]
        [row] = body["searches"]
        assert row["interrupted"] and row["leads"] == 0
        assert row["reason"].endswith("before it found any businesses.")
        assert daily.claim({"location": "84101"})[0] == daily.today()
    finally:
        finish()


def test_starting_a_search_finishes_the_cut_off_one_first(monkeypatch):
    from leadgen.pipeline import RunResult

    _, finish = _start(monkeypatch)
    try:
        interrupted._shutting_down()
        interrupted._live.clear()
        monkeypatch.setattr(web.finding, "run", lambda params, progress: RunResult(
            [], (40.76, -111.89), "SLC", [], {}))
        client = web.create_app().test_client()
        res = client.post("/search", data={"location": "84101", "radius": "30"})
        assert res.status_code == 200, res.get_json()
        assert len(saved.load()) == 2
    finally:
        finish()


def test_a_search_that_ends_removes_its_working_rows(monkeypatch):
    from leadgen.pipeline import RunResult

    seen = {}

    def run(params, progress):
        report_found(list(FOUND))
        with store.connect() as db:
            seen["runs"] = db.all("SELECT day FROM search_runs")
        return RunResult(list(FOUND), (40.76, -111.89), "SLC", [], {})
    monkeypatch.setattr(web.finding, "run", run)
    client = web.create_app().test_client()
    job = client.post("/search", data={"location": "84101", "radius": "30"}).get_json()["job_id"]
    for _ in range(200):
        if client.get(f"/status/{job}").get_json()["state"] != "running":
            break
        time.sleep(0.05)
    assert seen["runs"] == [(daily.today(),)]
    for _ in range(100):
        if not interrupted._live:
            break
        time.sleep(0.02)
    with store.connect() as db:
        assert db.all("SELECT id FROM search_runs") == [] == db.all("SELECT id FROM search_found")
    assert interrupted.recover() == 0 and not interrupted.pending()


def test_a_run_whose_search_had_finished_is_only_tidied(monkeypatch):
    from leadgen.pipeline import SearchParams

    day, _ = daily.claim({"location": "84101"})
    run = interrupted.Run(day, SearchParams(center=(40.76, -111.89), place="SLC"))
    run.start()
    run.add(list(FOUND))
    daily.finish(day, {"leads": 5})          # the search finished; its server stopped before tidying
    interrupted._shutting_down()
    interrupted._live.clear()
    assert interrupted.recover() == 1
    assert daily.info(day)["leads"] == 5 and saved.load() == []
    with store.connect() as db:
        assert db.all("SELECT id FROM search_runs") == []


def test_the_sources_report_what_they_return_as_they_go(monkeypatch):
    got = []
    # The map data: each area as it answers.
    monkeypatch.setattr(osm, "request_json", lambda *a, **k: {"elements": [
        {"type": "node", "id": 9, "lat": 40.7, "lon": -111.9,
         "tags": {"name": "Acme Foods", "industrial": "food"}}]})
    monkeypatch.setattr(config, "OVERPASS_ENDPOINTS", config.OVERPASS_ENDPOINTS[:1])
    with collecting(got.extend):
        osm.search(40.7, -111.9, 2)
    assert [l.name for l in got] == ["Acme Foods"]
    # A paid source: each results page.
    got.clear()

    def page(query, cell, token):
        return [{"name": f"{query} {token or 1}"}], (2 if token is None else None)

    def parse(item, query):
        return Lead(name=item["name"], lat=40.7, lon=-111.9, source="yelp", source_id=item["name"])
    with collecting(got.extend):
        paging.run_searches("Yelp", ["grocery"], [(40.7, -111.9, 5.0)], page, parse, max_pages=2,
                            max_requests=5, cache_version="t", cache=_NoCache())
    assert [l.name for l in got] == ["grocery 1", "grocery 2"]
    # Outside a search nothing is collected.
    report_found([FOUND[0]])


class _NoCache:
    def get(self, key, ttl=None):
        return None

    def put(self, key, value, ttl=None):
        pass
