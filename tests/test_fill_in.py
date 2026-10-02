"""Map areas a day's search missed are filled in in the background, within the same
search: their businesses join the saved list as they arrive, the day's record says
how it stands, and the day's one search is used only once."""

import re
import threading
import time

import pytest
from test_map_data import _answer, _box_of, _wait

from leadgen import config, daily, fillin, pipeline, web
from leadgen.http import HttpError
from leadgen.sources import osm


@pytest.fixture
def filling(monkeypatch):
    monkeypatch.setattr(fillin, "ON", True)
    monkeypatch.setattr(config, "FILL_IN_PAUSE_SECONDS", 0)
    monkeypatch.setattr(config, "OVERPASS_ENDPOINTS", config.OVERPASS_ENDPOINTS[:1])
    monkeypatch.setattr(pipeline, "geocode", lambda location, key: (40.76, -111.89, "Salt Lake City"))


def _servers(fail_times):
    """Mirrors that refuse every query about the northern third of the area the first
    `fail_times` times each part is asked, then answer (one business per part)."""
    tries: dict = {}

    def servers(method, url, data, **kw):
        box = _box_of(data["data"])
        if box[0] > 40.8:
            tries[box] = tries.get(box, 0) + 1
            if tries[box] <= fail_times:
                raise HttpError(f"{url}: ConnectionResetError(104, 'Connection reset by peer')")
        return _answer(box)
    return servers


def _search(client):
    job = client.post("/search", data={"location": "84101", "radius": "30"}).get_json()["job_id"]
    body = _wait(client, job)
    for _ in range(400):
        if not fillin.running:
            return body
        time.sleep(0.05)
    raise AssertionError("the filling in never finished")


def test_missing_map_areas_are_filled_in_later_and_the_search_ends_complete(filling, monkeypatch):
    # Every northern quarter fails in the search's first round and its catch-up round,
    # and answers when the background asks again.
    monkeypatch.setattr(osm, "request_json", _servers(fail_times=1 + config.OVERPASS_RETRY_ROUNDS))
    client = web.create_app().test_client()
    body = _search(client)
    assert body["state"] == "done" and body["found"] == 6
    assert "being asked again in the background" in body["note"]

    history = client.get("/searches").get_json()
    record = history["searches"][0]
    assert len(history["searches"]) == 1 and history["used_today"]
    assert record["fill"]["state"] == "complete" and record["fill"]["left"] == 0
    # The 12 quarters' businesses, less the corner ones outside the 30 miles.
    found = record["fill"]["found"]
    assert 8 <= found < 12 and record["fill"]["new"] == found
    assert not record["partial"] and record["leads"] == 6 + found and record["new"] == 6 + found
    assert not any("part of the area" in w for w in record["warnings"])
    assert any(w.startswith("Complete: the map areas") for w in record["warnings"])
    details = dict(record["details"])
    assert details["Free map data: filled in later, in the background"] == \
        f"{found} businesses ({found} new), every area in"
    assert len(client.get("/leads").get_json()["leads"]) == 6 + found
    # Still today's one search.
    again = client.post("/search", data={"location": "84101"})
    assert again.status_code == 409


def test_areas_that_never_answer_are_reported_after_the_hour(filling, monkeypatch):
    monkeypatch.setattr(config, "FILL_IN_SECONDS", 0.3)
    monkeypatch.setattr(osm, "request_json", _servers(fail_times=10_000))
    client = web.create_app().test_client()
    body = _search(client)
    assert body["state"] == "done" and "being asked again" in body["note"]
    # The search's own note names the towns still missing when it ends.
    assert re.search(r"\(about 6 of 9 areas searched; not yet: (Centerville|Kaysville)", body["note"])
    record = client.get("/searches").get_json()["searches"][0]
    assert record["fill"]["state"] == "gave_up" and record["fill"]["left"] == 3
    assert record["fill"]["rounds"] >= 1 and record["partial"] and record["leads"] == 6
    # The towns the missing (northern) areas hold are named, the nearest first.
    where = record["fill"]["where"]
    assert where.split(", ")[0] in ("Centerville", "Kaysville") and "Salt Lake City" not in where
    assert any(f"3 areas of the free map data (around {record['fill']['where']}) never answered "
               "today" in w for w in record["warnings"])
    assert not any("being asked again" in w for w in record["warnings"])


def test_pausing_searches_stops_the_filling_in(filling, monkeypatch):
    monkeypatch.setattr(osm, "request_json", _servers(fail_times=10_000))
    real = config.stop_reason

    def paused_for_the_background(source=None):
        if threading.current_thread().name.startswith("fill-in"):
            return "the administrator paused searching"
        return real(source)
    monkeypatch.setattr(config, "stop_reason", paused_for_the_background)
    client = web.create_app().test_client()
    _search(client)
    record = client.get("/searches").get_json()["searches"][0]
    assert record["fill"]["state"] == "stopped" and record["fill"]["rounds"] == 0
    assert any(re.search(r"stopped because searching was paused; 3 areas \(around (Centerville|Kaysville)"
                         r"[^)]*\) never answered\.", w) for w in record["warnings"])


def test_a_filling_in_cut_short_by_a_restart_reads_as_interrupted():
    day, _ = daily.claim({"location": "84101"})
    daily.finish(day, {"leads": 6, "partial": True,
                       "fill": {"state": "filling", "left": 3, "areas": 9, "found": 0, "new": 0,
                                "rounds": 0, "until": time.time() - daily.FILL_GRACE_SECONDS - 5}})
    record = daily.history()["current"]
    assert record["fill"]["state"] == "interrupted" and record["fill"]["until_text"]
    daily.finish(day, {"fill": {**record["fill"], "state": "filling", "until": time.time() + 60}})
    assert daily.history()["current"]["fill"]["state"] == "filling"


def test_the_background_rounds_ask_only_the_missing_parts(monkeypatch):
    asked = []

    def servers(method, url, data, **kw):
        box = _box_of(data["data"])
        asked.append(box)
        return _answer(box)
    monkeypatch.setattr(osm, "request_json", servers)
    parts = [((40.9, -112.0, 41.0, -111.9), 0.05, 1)]
    found, left = osm.fill_in(40.76, -111.89, 30, [], parts, 30)
    assert asked == [parts[0][0]] and len(found) == 1 and left == []
    assert osm.areas_left([((0, 0, 1, 1), 0.25, 1)] * 2, 9) == 4
    assert osm.areas_left([], 9) == 0


def test_a_fill_in_past_midnight_stays_in_view_and_ends_before_the_next_days_search(filling, monkeypatch):
    monkeypatch.setattr(config, "FILL_IN_PAUSE_SECONDS", 0.05)
    monkeypatch.setattr(osm, "request_json", _servers(fail_times=10_000))
    client = web.create_app().test_client()
    first_day = daily.today()
    job = client.post("/search", data={"location": "84101", "radius": "30"}).get_json()["job_id"]
    assert _wait(client, job)["state"] == "done" and first_day in fillin.running
    # Midnight passes while the missed areas are still being asked again.
    next_day = "2999-01-01"
    monkeypatch.setattr(daily, "today", lambda: next_day)
    body = client.get("/searches").get_json()
    assert body["current"] is None and not body["used_today"]
    assert body["filling"]["day"] == first_day and body["filling"]["fill"]["state"] == "filling"
    # The next day's search ends it before its own map step starts.
    still_running = []
    real_run = web.finding.run

    def run(params, progress):
        still_running.append(sorted(fillin.running))
        return real_run(params, progress)
    monkeypatch.setattr(web.finding, "run", run)
    job = client.post("/search", data={"location": "84101", "radius": "30"}).get_json()["job_id"]
    assert _wait(client, job)["state"] == "done"
    assert still_running == [[]]
    record = next(r for r in client.get("/searches").get_json()["searches"] if r["day"] == first_day)
    assert record["fill"]["state"] == "stopped" and record["fill"]["why"] == fillin.NEW_SEARCH
    assert any("stopped because the next day's search started" in w for w in record["warnings"])
    # The new day's own fill-in (its areas fail too) is ended here, as the test is over.
    fillin.end_others("", wait=10)
    assert not fillin.running
