"""Map areas a day's search missed are filled in in the background, within the same
search: their businesses join the saved list as they arrive, the Find page's one
coverage status says how it stands, and the day's one search is used only once."""

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


# What a note would say if it repeated the coverage status (it never does).
_COVERAGE_WORDS = re.compile(r"part of the area|map areas|parts? of the|areas? (?:searched|asked)|asked again")


def _no_coverage(notes):
    return not any(_COVERAGE_WORDS.search(note) for note in notes)


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
    # Nothing beside the result: the Find page's one coverage status says how it stands.
    assert body["note"] is None and _no_coverage(body["warnings"])

    history = client.get("/searches").get_json()
    record = history["searches"][0]
    assert len(history["searches"]) == 1 and history["used_today"]
    assert record["fill"]["state"] == "complete" and record["fill"]["left"] == 0
    # The parts the search missed are kept, and the filling in's own list empties (the Map page).
    assert len(record["map_areas"]["missing"]) >= 3 and record["fill"]["boxes"] == []
    # The 12 quarters' businesses, less the corner ones outside the 30 miles.
    found = record["fill"]["found"]
    assert 8 <= found < 12 and record["fill"]["new"] == found
    assert not record["partial"] and record["leads"] == 6 + found and record["new"] == 6 + found
    assert _no_coverage(record["warnings"])
    coverage = record["coverage"]
    assert coverage["state"] == "complete" and not coverage["filling"] and coverage["found"] == found
    assert coverage["text"] == ("Covered: the whole 30-mile area. The last parts came in later, in the "
                                f"background ({found} more businesses, {found} new). Everything found is on "
                                "the Leads page, ready to call.")
    details = dict(record["details"])
    assert details["Free map data: area covered"] == \
        f"All 9 parts ({found} businesses came in later, in the background)"
    # Internal counts (how many parts were asked again) are never shown.
    assert not any("asked again" in str(label) or "filled in later" in str(label) for label in details)
    assert history["current"]["coverage"] == coverage
    assert len(client.get("/leads").get_json()["leads"]) == 6 + found
    # Still today's one search.
    again = client.post("/search", data={"location": "84101"})
    assert again.status_code == 409


def test_areas_that_never_answer_are_reported_after_the_hour(filling, monkeypatch):
    from leadgen import alerts
    alerts_sent: list[str] = []
    monkeypatch.setattr(alerts, "report", lambda kind, text, **kw: alerts_sent.append(text))
    monkeypatch.setattr(config, "FILL_IN_SECONDS", 0.3)
    monkeypatch.setattr(osm, "request_json", _servers(fail_times=10_000))
    client = web.create_app().test_client()
    body = _search(client)
    assert body["state"] == "done" and body["note"] is None and _no_coverage(body["warnings"])
    record = client.get("/searches").get_json()["searches"][0]
    assert record["fill"]["state"] == "gave_up" and record["fill"]["left"] == 3
    assert record["fill"]["boxes"] and all(box[0] > 40.8 for box in record["fill"]["boxes"])   # the north
    assert record["fill"]["rounds"] >= 1 and record["partial"] and record["leads"] == 6
    # The towns the missing (northern) parts hold are named, the nearest first.
    where = record["fill"]["where"]
    assert where.split(", ")[0] in ("Centerville", "Kaysville") and "Salt Lake City" not in where
    # Said once, in one set of words: how much was covered, which towns are missing, and
    # that what was found can be called now.
    assert record["coverage"]["text"] == (
        f"Covered: about 6 of the 9 parts of the 30-mile area. {where} didn't come in today, so businesses "
        "there are missing until the next search. The 6 businesses already found are on the Leads page, "
        "ready to call now.")
    assert _no_coverage(record["warnings"])
    assert dict(record["details"])["Free map data: area covered"] == f"About 6 of 9 parts; {where} didn't come in"
    # The administrator's problem list says it too (in the same words: parts, not areas).
    problems = [p for p in alerts_sent if "stayed incomplete" in p]
    assert problems == [f"Today's search stayed incomplete: 3 of the 9 parts of the area (around {where}) "
                        "never came in from the free map data today, so businesses there are missing until "
                        "the next search."]


def test_pausing_searches_stops_the_filling_in(filling, monkeypatch):
    monkeypatch.setattr(osm, "request_json", _servers(fail_times=10_000))
    real = config.stop_reason

    def paused_for_the_background(source=None):
        if threading.current_thread().name.startswith("leadgen fill-in"):
            return "the administrator paused searching"
        return real(source)
    monkeypatch.setattr(config, "stop_reason", paused_for_the_background)
    client = web.create_app().test_client()
    _search(client)
    record = client.get("/searches").get_json()["searches"][0]
    assert record["fill"]["state"] == "stopped" and record["fill"]["rounds"] == 0
    # The parts left were not asked again: the page never blames the map servers for them.
    where = record["fill"]["where"]
    assert re.match(r"Centerville|Kaysville", where)
    assert record["coverage"]["text"] == (
        "Covered: about 6 of the 9 parts of the 30-mile area. Filling in stopped because searching was paused, "
        f"so these weren't searched today: {where}. The 6 businesses already found are on the Leads page, "
        "ready to call now.")
    assert dict(record["details"])["Free map data: area covered"] == \
        f"About 6 of 9 parts; not searched: {where} (searching was paused)"
    text = " ".join([record["coverage"]["text"], *record["warnings"], *map(str, dict(record["details"]).values())])
    assert "didn't come in" not in text and "never" not in text


def test_a_filling_in_cut_short_by_a_restart_reads_as_interrupted():
    day, _ = daily.claim({"location": "84101"})
    daily.finish(day, {"leads": 6, "partial": True,
                       "fill": {"state": "filling", "left": 3, "areas": 9, "found": 0, "new": 0,
                                "rounds": 0, "until": time.time() - daily.FILL_GRACE_SECONDS - 5}})
    record = daily.history()["current"]
    assert record["fill"]["state"] == "interrupted" and record["fill"]["until_text"]
    assert fillin.coverage(record)["text"].startswith(
        "Covered: about 6 of the 9 parts of the area. A server restart cut the filling in short, so businesses in "
        "the rest of the area are missing until the next search.")
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
    # The Find page's status says it is the earlier search's, and that it stops when today's starts.
    text = body["filling"]["coverage"]["text"]
    assert text.startswith("The search of ") and "is still filling in. Covered so far: about 6 of the 9" in text
    assert text.endswith("It stops when today's search starts.")
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
    assert "Filling in stopped because the next day's search started" in record["coverage"]["text"]
    # The new day's own fill-in (its areas fail too) is ended here, as the test is over.
    fillin.end_others("", wait=10)
    assert not fillin.running


@pytest.mark.parametrize("how", ["site switch", "environment"])
def test_pausing_searching_ends_a_fill_in_within_seconds(filling, monkeypatch, how):
    """Between its rounds (5 minutes apart) a fill-in looks at Pause searching every few
    seconds: switched on, by the administrator's switch or the server's setting, the
    fill-in ends within seconds, its record says why, and no more map areas are asked."""
    from leadgen import switches
    monkeypatch.setattr(config, "FILL_IN_PAUSE_SECONDS", 300)
    asked = []
    servers = _servers(fail_times=10_000)

    def counted(method, url, data, **kw):
        asked.append(url)
        return servers(method, url, data, **kw)
    monkeypatch.setattr(osm, "request_json", counted)
    client = web.create_app().test_client()
    job = client.post("/search", data={"location": "84101", "radius": "30"}).get_json()["job_id"]
    assert _wait(client, job)["state"] == "done"
    current = client.get("/searches").get_json()["current"]
    assert current["fill"]["state"] == "filling" and current["coverage"]["filling"]
    assert re.match(r"Covered so far: about 6 of the 9 parts of the 30-mile area\. Still filling in, in the "
                    r"background until about \d+:\d\d [AP]M: (Centerville|Kaysville)", current["coverage"]["text"])
    assert current["coverage"]["text"].endswith("The 6 businesses already found are on the Leads page, ready to "
                                                "call now; new ones from the rest of the area will appear there "
                                                "on their own.")
    before = len(asked)
    if how == "site switch":
        switches.set_switch(config.SEARCH_PAUSED_ENV, True, "Matt")
    else:
        monkeypatch.setenv(config.SEARCH_PAUSED_ENV, "1")
    paused = time.monotonic()
    seen = set()
    while time.monotonic() - paused < 10:
        current = client.get("/searches").get_json()["current"]
        fill = current["fill"]
        if fill["state"] != "filling":
            break
        seen.add(current["coverage"]["text"].split(". ")[1])
        time.sleep(0.1)
    took = time.monotonic() - paused
    assert fill["state"] == "stopped" and fill["why"] == fillin.PAUSED and took < 10, took
    record = client.get("/searches").get_json()["current"]
    assert "Filling in stopped because searching was paused" in record["coverage"]["text"]
    # Until it ends, the status says it is stopping (not still filling in).
    assert all(said.startswith("Stopping the filling in of ") for said in seen)
    for _ in range(100):
        if not fillin.running:
            break
        time.sleep(0.05)
    assert not fillin.running and len(asked) == before         # no more map requests


# ---- records saved by earlier versions read the same way (nothing rewrites them)

def _partial_record(**fill):
    """A day's record as an earlier version wrote it for a 30-mile search with 2 of 9
    map areas answering (its notes and details said the coverage several ways)."""
    fill = {"state": "complete", "left": 0, "start_left": 7, "areas": 9, "found": 559, "new": 554,
            "rounds": 3, "where": "", **fill}
    coverage = ("about 2 of 9 areas searched; not yet: Salt Lake City, West Valley City, Herriman, "
                "Draper and more")
    reason = (f"The map data service answered for only part of the area ({coverage}), so some of its "
              "businesses are missing from this search; the 418 businesses found were saved.")
    return {"day": "2026-09-29", "leads": 977, "radius": 30, "partial": True, "reason": reason, "fill": fill,
            "details": {"osm raw results": 795, "osm areas searched": "about 2 of 9 areas",
                        "osm areas asked again": "7 (some never answered)",
                        "osm areas filled in later": "559 businesses (554 new), every area in"},
            "warnings": [f"{reason} Today's search is used up all the same (one search a day).",
                         fillin.NOTE, "Complete: the map areas that didn't answer at first were filled in later."]}


def _page(record):
    from leadgen.web import finding
    return finding._for_page(record, paused=False)


def test_a_complete_filling_in_reads_as_the_whole_area():
    record = _page(_partial_record())
    text = " ".join([record["coverage"]["text"], *record["warnings"], *map(str, dict(record["details"]).values())])
    assert "Salt Lake City" not in text and "not yet" not in text and "never answered" not in text
    assert record["warnings"] == []
    assert record["coverage"]["text"].startswith("Covered: the whole 30-mile area.")
    details = dict(record["details"])
    assert details["Free map data: area covered"] == "All 9 parts (559 businesses came in later, in the background)"
    assert not any("asked again" in label or "filled in later" in label for label in details)
    # The stored record is unchanged (the page's words are worked out each time).
    assert _partial_record()["details"]["osm areas asked again"] == "7 (some never answered)"


def test_a_partly_filled_in_search_names_one_list_of_missing_towns():
    where = "Kaysville, Farmington, Centerville and more"
    record = _page(_partial_record(state="gave_up", left=6, found=120, new=118, where=where))
    assert dict(record["details"])["Free map data: area covered"] == f"About 3 of 9 parts; {where} didn't come in"
    assert record["coverage"]["text"].startswith(f"Covered: about 3 of the 9 parts of the 30-mile area. {where} "
                                                 "didn't come in today")
    assert "Salt Lake City" not in " ".join([record["coverage"]["text"], *record["warnings"]])


def test_parts_left_by_a_pause_read_as_not_searched():
    where = "Kaysville and Farmington"
    stored = _partial_record(state="stopped", why="paused", left=6, found=120, new=118, where=where)
    # As an earlier version wrote it.
    stored["warnings"].append("Filling in the missing map areas was stopped because searching was paused; "
                              f"6 areas (around {where}) never answered.")
    record = _page(stored)
    text = " ".join([record["coverage"]["text"], *record["warnings"], *map(str, dict(record["details"]).values())])
    assert "never answered" not in text and "Salt Lake City" not in text and record["warnings"] == []
    assert f"so these weren't searched today: {where}." in record["coverage"]["text"]
    # The same record read twice (today's search is also in the history) says the same.
    assert _page(stored) == record


def test_a_search_that_never_filled_in_names_the_towns_its_reason_named():
    stored = _partial_record()
    del stored["fill"]
    record = _page(stored)
    assert record["coverage"]["state"] == "partial"
    assert record["coverage"]["text"].startswith(
        "Covered: about 2 of the 9 parts of the 30-mile area. Salt Lake City, West Valley City, Herriman, Draper "
        "and more didn't come in today")
    assert record["reason"].startswith("The free map data covered only about 2 of the 9 parts of the area "
                                       "(missing: Salt Lake City, West Valley City, Herriman, Draper and more)")


def test_another_sources_problem_keeps_its_note_without_the_map_clause():
    note = ("Couldn't reach Google, so its businesses are missing and the map data service answered for only "
            "part of the area (about 6 of 9 areas searched), so some of its businesses are missing from this "
            "search; the 6 businesses found were saved.")
    assert fillin.plain_notes([note, fillin.NOTE]) == [
        "Couldn't reach Google, so its businesses are missing from this search; the 6 businesses found were saved."]


def test_a_record_without_a_coverage_problem_has_no_status():
    record = {"day": "2026-09-29", "leads": 3, "details": {"leads kept": 3}}
    assert fillin.coverage(record) is None
    assert _page(record)["coverage"] is None
