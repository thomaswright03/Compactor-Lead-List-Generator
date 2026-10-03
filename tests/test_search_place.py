"""Where a search runs: the place is found (in Utah first) and shown before anything is
spent, a place outside AARCO's area needs a second yes, the history names the place, the
administrator's pause stops the free map step of a running search, and leads saved from a
search around the wrong place can be taken out of the list, only on request."""

import re
import threading
import time

import pytest

from leadgen import alerts, cleanup, cli, config, daily, geo, marks, pipeline, saved, stats, web
from leadgen.http import HttpError
from leadgen.models import Lead
from leadgen.pipeline import RunResult, SearchParams
from leadgen.progress import MAP, Step
from leadgen.scoring import score_lead
from leadgen.sources import osm

PORTLAND = (45.5152, -122.6784, "Portland, Multnomah County, Oregon, United States")


def _wait(client, job):
    for _ in range(400):
        body = client.get(f"/status/{job}").get_json()
        if body["state"] != "running":
            return body
        time.sleep(0.05)
    raise AssertionError("the search never finished")


def _lead(name, sid, lat=40.72, lon=-111.9, **kw):
    lead = Lead(name=name, lat=lat, lon=lon, source="osm", source_id=sid,
                raw_categories=kw.pop("raw_categories", ["shop=supermarket"]), **kw)
    return score_lead(lead, config.DEFAULT_KEYWORDS)


# ---- the place is looked for in Utah first

@pytest.mark.parametrize("typed, label", [("Murray", "Murray, UT"), ("sandy", "Sandy, UT"),
                                          ("Draper", "Draper, UT"), ("Midvale, UT", "Midvale, UT"),
                                          ("West Jordan, Utah", "West Jordan, UT"),
                                          ("Cottonwood Heights", "Cottonwood Heights, UT")])
def test_salt_lake_area_towns_typed_bare_are_the_utah_ones(monkeypatch, typed, label):
    monkeypatch.setattr(geo, "request_json", lambda *a, **k: pytest.fail("looked up online"))
    lat, lon, found = geo.geocode(typed, "")
    assert found == label
    assert geo.miles_from_aarco(lat, lon) < 20


def test_other_places_are_looked_for_in_utah_first(monkeypatch):
    asked = []

    def nominatim(method, url, params=None, **kw):
        asked.append(dict(params))
        if params.get("bounded"):            # the Utah-only lookup
            return [{"lat": "40.6", "lon": "-111.9", "class": "highway", "display_name": "Holly Lane, Utah"},
                    {"lat": "40.64", "lon": "-111.94", "class": "place", "display_name": "Hollyville, Utah"}]
        return [{"lat": "33.0", "lon": "-84.0", "display_name": "Somewhere, Georgia"}]
    monkeypatch.setattr(geo, "request_json", nominatim)
    lat, lon, label = geo.geocode("Hollyville", "")
    assert label == "Hollyville, Utah" and (lat, lon) == (40.64, -111.94)   # the town, not the street
    assert asked[0]["bounded"] == 1 and asked[0]["viewbox"] == "-114.06,42.01,-109.04,36.99"
    # Nothing in Utah: anywhere in the US (and the page then shows how far that is).
    asked.clear()
    monkeypatch.setattr(geo, "request_json", lambda m, u, params=None, **k: (
        asked.append(dict(params)) or ([] if params.get("bounded") else
                                       [{"lat": "33.0", "lon": "-84.0", "display_name": "Somewhere, Georgia"}])))
    assert geo.geocode("Somewhere", "")[2] == "Somewhere, Georgia"
    assert [bool(p.get("bounded")) for p in asked] == [True, False]


def test_a_place_in_another_state_is_not_looked_for_in_utah(monkeypatch):
    asked = []
    monkeypatch.setattr(geo, "request_json", lambda m, u, params=None, **k: (
        asked.append(dict(params)) or [{"lat": "45.5", "lon": "-122.7", "display_name": "Portland, Oregon"}]))
    for typed in ("Portland, OR", "Murray, Kentucky", "Austin TX 78701".replace(" TX", ", TX")):
        asked.clear()
        geo.geocode(typed, "")
        assert not any(p.get("bounded") for p in asked), typed
    assert geo.names_another_state("Portland Oregon") and not geo.names_another_state("Murray")
    assert not geo.names_another_state("Sandy, UT") and not geo.names_another_state("Provo, Utah")


def test_google_lookups_prefer_utah(monkeypatch):
    sent = []

    def google(method, url, params=None, **kw):
        sent.append(params)
        return {"status": "OK", "results": [{"geometry": {"location": {"lat": 40.66, "lng": -111.88}},
                                             "formatted_address": "Murray, UT, USA"}]}
    monkeypatch.setattr(geo, "request_json", google)
    assert geo.geocode("4800 S State St", "KEY")[2] == "Murray, UT, USA"
    assert sent[0]["bounds"] == "36.99,-114.06|42.01,-109.04" and sent[0]["components"] == "country:US"


# ---- the page sees the place (and its distance) before anything is spent

def test_the_place_is_shown_with_its_distance_from_aarco():
    client = web.create_app().test_client()
    body = client.get("/place?location=Murray").get_json()
    assert body["label"] == "Murray, UT" and not body["outside"]
    assert 4 < body["miles"] < 7 and body["area_miles"] == config.SERVICE_AREA_MILES
    aarco = client.get("/place?location=876 Fortune Rd").get_json()
    assert aarco["miles"] == 0 and aarco["label"].startswith("AARCO Compactor")
    blank = client.get("/place?location=")
    assert blank.status_code == 400 and blank.get_json()["field"] == "location"
    assert not daily.history()["searches"]


def test_a_place_outside_aarcos_area_needs_a_second_yes(monkeypatch):
    """A far place is refused (nothing spent, the day not claimed) until the page confirms
    that very place; then it runs there, and the history names where it ran."""
    monkeypatch.setattr(pipeline, "geocode", lambda location, key: PORTLAND)
    ran = []

    def run(params, progress):
        ran.append(params)
        return RunResult([], params.center, params.place, [], {"leads kept": 0})
    monkeypatch.setattr(web.finding, "run", run)
    client = web.create_app().test_client()
    place = client.get("/place?location=Portland").get_json()
    assert place["outside"] and place["miles"] > 600
    res = client.post("/search", data={"location": "Portland"})
    body = res.get_json()
    assert res.status_code == 409 and body["confirm_far"] and body["place"]["label"] == PORTLAND[2]
    assert "is 633 miles from AARCO's shop, outside AARCO's area" in body["error"]
    assert not ran and not daily.history()["searches"] and not client.get("/searches").get_json()["used_today"]
    # A confirmation for another place doesn't count.
    assert client.post("/search", data={"location": "Portland", "confirm_place": "40.7,-111.9"}).status_code == 409
    assert client.post("/search", data={"location": "Portland", "confirm_place": "junk"}).status_code == 409
    job = client.post("/search", data={"location": "Portland", "confirm_place": place["confirm"]}).get_json()
    assert _wait(client, job["job_id"])["state"] == "done"
    assert ran[0].center == (PORTLAND[0], PORTLAND[1]) and ran[0].place == PORTLAND[2]
    record = client.get("/searches").get_json()["searches"][0]
    assert record["location"] == "Portland" and record["place"] == PORTLAND[2] and record["miles"] > 600


def test_a_search_in_the_area_runs_where_the_page_showed(monkeypatch):
    ran = []
    monkeypatch.setattr(web.finding, "run", lambda params, progress: ran.append(params) or RunResult(
        [], params.center, params.place, [], {"leads kept": 0}))
    client = web.create_app().test_client()
    job = client.post("/search", data={"location": "Murray"}).get_json()["job_id"]
    assert _wait(client, job)["location"] == "Murray, UT"
    assert ran[0].place == "Murray, UT"
    record = client.get("/searches").get_json()["current"]
    assert record["place"] == "Murray, UT" and record["miles"] < 7


def test_the_run_uses_the_place_found_without_looking_it_up_again(monkeypatch):
    monkeypatch.setattr(pipeline, "geocode", lambda *a: pytest.fail("looked up again"))
    monkeypatch.setattr(pipeline.osm, "search", lambda *a, **k: ([_lead("Costco", "c")], []))
    res = pipeline.run(SearchParams(location="Murray", source="osm", center=(40.6669, -111.888),
                                    place="Murray, UT"))
    assert res.location_label == "Murray, UT" and res.center == (40.6669, -111.888)
    assert "center" not in res.run_info(SearchParams()) and "place" not in res.run_info(SearchParams())


# ---- pausing searching stops the free map step of a running search

def test_pausing_stops_the_map_data_step_within_seconds(monkeypatch):
    monkeypatch.setattr(osm, "STOP_CHECK_SECONDS", 0.2)
    monkeypatch.setattr(config, "OVERPASS_ENDPOINTS", config.OVERPASS_ENDPOINTS[:2])
    asked, paused, answered = [], threading.Event(), threading.Event()

    def slow(method, url, data, **kw):
        asked.append(url)
        if len(asked) == 2:
            return {"elements": [{"type": "node", "id": 1, "lat": 40.7, "lon": -111.9,
                                  "tags": {"name": "Market 1", "shop": "supermarket"}}]}
        paused.set()                    # the administrator pauses while this part is asked
        answered.wait(3)                # a hanging server (it answers once the test is over)
        return {"elements": []}
    monkeypatch.setattr(osm, "request_json", slow)
    stop = lambda: "the administrator paused searching" if paused.is_set() else None  # noqa: E731
    said = []
    start = time.monotonic()
    with pytest.raises(osm.Stopped) as got:
        osm.search(40.76, -111.89, 30, progress=said.append, stop=stop)
    took = time.monotonic() - start
    assert took < 2.5, took                                   # not the 3 s the slow part takes
    assert got.value.reason == "the administrator paused searching"
    assert [l.name for l in got.value.leads] == ["Market 1"]  # what had answered is kept
    n = len(asked)
    time.sleep(0.5)
    assert len(asked) == n and n <= 3                         # nothing more is asked
    assert said[-1] == "OpenStreetMap: stopped, the administrator paused searching"
    assert web.finding.plain_progress(said[-1]).startswith("Stopping: the administrator paused searching")
    # The query left waiting on the hanging server ends with the test.
    answered.set()
    for thread in threading.enumerate():
        if thread.name.startswith("leadgen map server"):
            thread.join(5)


def test_a_search_paused_during_the_map_data_says_so_and_keeps_what_it_found(monkeypatch):
    def osm_search(lat, lon, radius, keywords=(), progress=None, stats=None, stop=None):
        assert stop() is None
        monkeypatch.setenv(config.SEARCH_PAUSED_ENV, "1")          # paused mid-way
        assert stop() == "the administrator paused searching"
        raise osm.Stopped(stop(), [_lead("Costco", "c")], [])
    monkeypatch.setattr(pipeline.osm, "search", osm_search)
    client = web.create_app().test_client()
    job = client.post("/search", data={"location": "Murray", "source": "osm"}).get_json()["job_id"]
    body = _wait(client, job)
    assert body["state"] == "done" and body["saved"] and body["stopped"] == "the administrator paused searching"
    assert body["note"].startswith("Stopped by the administrator (the administrator paused searching): "
                                   "the 1 businesses it had found were saved")
    assert any("stopped by the administrator partway through the free map data" in w for w in body["warnings"])
    assert saved.count() == 1
    history = client.get("/searches").get_json()
    assert history["current"]["stopped"] == "the administrator paused searching" and history["used_today"]


def test_a_search_paused_before_the_map_found_anything_gives_the_day_back(monkeypatch):
    monkeypatch.setattr(alerts, "ON", True)
    monkeypatch.setattr(pipeline.osm, "search", lambda *a, **k: (_ for _ in ()).throw(
        osm.Stopped("the administrator paused searching", [], [])))
    client = web.create_app().test_client()
    job = client.post("/search", data={"location": "Murray", "source": "osm"}).get_json()["job_id"]
    body = _wait(client, job)
    assert body["state"] == "error" and "stopped by the administrator" in body["message"]
    assert body["stopped"] == "the administrator paused searching"
    history = client.get("/searches").get_json()
    assert not history["used_today"]
    # A deliberate stop is recorded as stopped, not as a failed search, and raises no alarm.
    record = history["searches"][0]
    assert record["stopped"] == "the administrator paused searching"
    assert alerts.recent()["count"] == 0


# ---- a place lookup that is down is not a spelling mistake

def _lookups_down(*a, **k):
    raise HttpError("https://nominatim.openstreetmap.org/search: ConnectionError(timed out)")


def test_a_lookup_outage_says_try_again_not_check_the_spelling(monkeypatch):
    monkeypatch.setattr(geo, "request_json", _lookups_down)
    client = web.create_app().test_client()
    res = client.get("/place?location=84111")             # a real ZIP code (not known offline)
    body = res.get_json()
    assert res.status_code == 503 and "field" not in body and body["lookup_down"]
    assert "Couldn't look up '84111' right now" in body["error"]
    assert "Nothing was spent, and today's search is still available" in body["error"]
    assert "spelling" not in body["error"]
    # Starting the search says the same, and the day isn't claimed.
    res = client.post("/search", data={"location": "Draperville"})
    assert res.status_code == 503 and "right now" in res.get_json()["error"]
    assert not client.get("/searches").get_json()["used_today"]


def test_a_place_that_doesnt_exist_still_reads_as_not_found(monkeypatch):
    def lookups(method, url, **kw):
        if "zippopotam" in url:
            raise HttpError(f"{url} returned HTTP 404: {{}}", 404)       # no such ZIP code
        return []                                                       # nothing found
    monkeypatch.setattr(geo, "request_json", lookups)
    res = web.create_app().test_client().get("/place?location=00000")
    assert res.status_code == 400 and res.get_json()["field"] == "location"
    assert "Check the spelling" in res.get_json()["error"]


def test_error_messages_are_short_full_sentences(monkeypatch):
    monkeypatch.setattr(geo, "request_json", lambda *a, **k: [])
    with pytest.raises(geo.GeocodeError) as err:
        geo.geocode("Nowhere " * 75, "")                # 600 characters typed
    assert len(str(err.value)) < 150 and "…'" in str(err.value)
    client = web.create_app().test_client()
    for form in ({"location": "84101", "radius": "500"}, {"location": "84101", "radius": "abc"},
                 {"location": "84101", "radius": "30", "min_score": "5.5"},
                 {"location": "84101", "source": "nowhere"}, {"location": "84101", "grid": "4"}):
        message = client.post("/search", data=form).get_json()["error"]
        assert message.endswith("."), message


# ---- the progress bar follows the map areas that answered

def test_the_map_steps_percent_follows_the_areas_done(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(web.finding.time, "time", lambda: clock[0])
    progress = web.finding._Progress({"skipped": [1]})

    def at(areas):            # the bar at `areas` of 17 done: locate 3, map 36, merge 8, save 6
        return 100 * (3 + 36 * areas / 17) / 53

    def areas(done, note=""):
        return Step(f"OpenStreetMap: ({done} of 17 areas done)", MAP, done, 17, note=note)
    progress(areas(0))
    assert progress.pct() < 10                        # starts in single digits
    progress(areas(3))
    clock[0] += 180                                   # three minutes, no area answers
    assert at(3) <= progress.pct() <= at(4)
    clock[0] += 3600
    assert progress.pct() < at(4)                     # never past the next area's mark
    progress(areas(9))
    assert at(9) <= progress.pct() <= at(10)
    # Parts asked again in smaller pieces, or in a catch-up round: the count stays.
    before = progress.pct()
    progress(areas(9, "retry"))
    assert progress.pct() == before
    assert progress.job["message"] == ("Searching the free map data: 9 of 17 parts of the area done (asking "
                                       "again for the parts the busy servers missed)…")
    progress(areas(17))
    assert progress.pct() == pytest.approx(at(17))


# ---- leads from a search around the wrong place: taken out only on request

def test_far_leads_are_listed_and_removed_only_on_request(capsys, monkeypatch):
    near = _lead("Smith's Marketplace", "n1")
    far = [_lead(f"SF Market {i}", f"f{i}", lat=37.77, lon=-122.42, city="San Francisco", state="CA")
           for i in range(3)]
    saved.save_search([near, *far], config.DEFAULT_KEYWORDS)
    by_name = {l.name: l for l in saved.load()}
    marks.set_mark(by_name["SF Market 0"].uid, "no")        # someone already worked on it
    assert cli.main(["out-of-area"]) == 0
    out = capsys.readouterr().out
    assert "3 saved leads farther than 60 miles from AARCO's shop (1 marked or called, never removed)" in out
    assert "San Francisco, CA" in out and "Nothing was changed" in out
    assert saved.count() == 4                                 # listing changes nothing
    assert cli.main(["out-of-area", "--remove"]) == 0
    assert "2 leads taken out of the saved list" in capsys.readouterr().out
    assert sorted(l.name for l in saved.load()) == ["SF Market 0", "Smith's Marketplace"]
    assert cli.main(["out-of-area", "--restore"]) == 0
    assert "2 removed leads put back" in capsys.readouterr().out
    assert saved.count() == 4
    assert cleanup.far_leads(1000) == []
    assert cli.main(["out-of-area", "--miles", "1000"]) == 0
    assert "No saved lead is farther than 1000 miles" in capsys.readouterr().out


def test_the_command_line_warns_about_a_place_outside_the_area(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(pipeline, "geocode", lambda location, key: PORTLAND)
    monkeypatch.setattr(pipeline.osm, "search", lambda *a, **k: ([], []))
    assert cli.main(["run", "--source", "osm", "-l", "Portland", "-q", "--out", str(tmp_path / "x.csv")]) == 0
    assert re.search(r"Warning: Portland.* is 633 miles from AARCO's shop, outside AARCO's area",
                     capsys.readouterr().err)


# ---- the Stats page's note on tier D

def test_stats_say_how_many_leads_each_tier_holds():
    weak = _lead("Acme Office", "w", raw_categories=["office=company"])
    strong = _lead("Costco", "c", raw_categories=["shop=wholesale", "brand=Costco"])
    assert weak.tier == "D" and strong.tier != "D"
    summary = stats.summarize([weak, strong])
    tiers = {t["tier"]: t for t in summary["by_tier"]}
    assert tiers["D"]["saved"] == 1 and tiers["D"]["checked"] == 0
    assert sum(t["saved"] for t in summary["by_tier"]) == 2
