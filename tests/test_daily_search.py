"""The day's one search: a search whose leads can't be saved gives the day back, an
incomplete search uses up the day (there is no same-day re-run), and shows one short note."""

import time

from leadgen import config, daily, pipeline, saved, store, web
from leadgen.models import Lead
from leadgen.pipeline import RunResult, SearchParams
from leadgen.scoring import score_lead
from leadgen.sources import SourceError, osm


def _lead(name="Costco", sid="c", **kw):
    kw.setdefault("lat", 40.72)
    kw.setdefault("lon", -111.9)
    kw.setdefault("raw_categories", ["shop=wholesale"])
    lead = Lead(name=name, source="osm", source_id=sid, **kw)
    score_lead(lead, config.DEFAULT_KEYWORDS)
    return lead


def _wait(client, job):
    for _ in range(400):
        body = client.get(f"/status/{job}").get_json()
        if body["state"] != "running":
            return body
        time.sleep(0.05)
    raise AssertionError("the search never finished")


def _map_lead(name, tags, sid=None, **kw):
    return Lead(name=name, lat=40.72, lon=-111.9, source="osm", source_id=sid or name,
                raw_categories=tags, **kw)


# ---- an incomplete search uses up the day: there is no same-day re-run

def test_an_incomplete_search_uses_up_the_day_with_no_rerun(monkeypatch):
    """One search a Utah day, the owner's rule: an incomplete search (Google refused its
    key) keeps the day, and a second search that day is refused."""
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "AIzaFAKEKEYFORTESTS000000000000000000")
    monkeypatch.setattr(pipeline, "geocode", lambda location, key: (40.72, -111.9, "Salt Lake City"))

    def refused(*args, **kw):
        raise SourceError("Google rejected the request. HTTP 403: denied")
    monkeypatch.setattr(pipeline.google_places, "search", refused)
    monkeypatch.setattr(pipeline.osm, "search", lambda *a, **k: ([_lead()], []))
    client = web.create_app().test_client()
    job = client.post("/search", data={"location": "84101"}).get_json()["job_id"]
    text = " ".join(_wait(client, job)["warnings"])
    assert "Today's search is used up all the same" in text and "run it again" not in text
    history = client.get("/searches").get_json()
    assert history["used_today"] and history["current"]["partial"] and "reruns_left" not in history
    again = client.post("/search", data={"location": "84101"})
    assert again.status_code == 409
    assert "the next search can run tomorrow, from midnight Utah time" in again.get_json()["error"]


# ---- a search whose leads can't be saved gives the day back

def test_leads_that_cannot_be_saved_give_the_day_back(monkeypatch):
    monkeypatch.setattr(web.finding, "run", lambda params, progress: RunResult(
        [_lead(), _lead("Walmart", "w")], (40.76, -111.89), "Salt Lake City", [],
        {"leads kept": 2}))

    def broken(*args, **kw):
        raise store.Unavailable("the database could not be reached")
    real_save = saved.save_search
    monkeypatch.setattr(saved, "save_search", broken)
    client = web.create_app().test_client()
    job = client.post("/search", data={"location": "84101"}).get_json()["job_id"]
    body = _wait(client, job)
    assert body["state"] == "error"
    assert "found 2 businesses, but they couldn't be saved" in body["message"]
    assert "Today's search was not used up" in body["message"]
    history = client.get("/searches").get_json()
    assert not history["used_today"]
    failed = history["searches"][0]
    assert failed["failed"] and "couldn't be saved" in failed["reason"]
    # Once the database answers again, the search can be run again the same day.
    monkeypatch.setattr(saved, "save_search", real_save)
    monkeypatch.setattr(web.finding, "run", lambda params, progress: RunResult(
        [_lead()], (40.76, -111.89), "Salt Lake City", [], {"leads kept": 1}))
    job = client.post("/search", data={"location": "84101"}).get_json()["job_id"]
    assert _wait(client, job)["saved"]
    assert client.get("/searches").get_json()["used_today"]


# ---- an incomplete search shows one note, the details behind "More"

def test_an_incomplete_search_gives_one_short_note(monkeypatch):
    monkeypatch.setattr(pipeline, "geocode", lambda location, key: (40.72, -111.9, "Salt Lake City"))

    def partial(*a, **k):
        lead = _map_lead("Costco", ["shop=wholesale"])
        raise osm.PartialResult("OpenStreetMap answered for only part of the area", [lead], [])
    monkeypatch.setattr(pipeline.osm, "search", partial)
    client = web.create_app().test_client()
    job = client.post("/search", data={"location": "84101"}).get_json()["job_id"]
    body = _wait(client, job)
    assert body["note"] == ("Some businesses are missing: the map data service answered for "
                            "only part of the area. Today's search is now used up; the next "
                            "one can run tomorrow.")
    assert len(body["warnings"]) >= 2                     # the details, shown behind "More"


def test_a_search_recorded_under_the_old_name_shows_aarco():
    """Searches run before the owner corrected the company's name (AARCO, not Arco) say
    AARCO in the history; the stored record keeps its text."""
    import json
    old = "Arco Compactor, 876 Fortune Rd, Salt Lake City, UT 84104"
    with store.connect() as db:
        db.run("INSERT INTO searches (day, at, info) VALUES (?, ?, ?)",
               ("2026-09-30", time.time() - 3 * 86400,
                json.dumps({"location": "876 Fortune Rd", "place": old, "found_near": old, "leads": 3})))
    [record] = daily.history()["searches"]
    assert record["place"] == record["found_near"] == "AARCO Compactor, 876 Fortune Rd, Salt Lake City, UT 84104"
    assert daily.earlier_search(record["place"], daily.today(), 0) is not None
    with store.connect() as db:
        assert json.loads(db.one("SELECT info FROM searches")[0])["place"] == old


# ---- what a search's record keeps for the Map page

def _params(source="auto"):
    return SearchParams(location=config.OWN_ADDRESS, radius_miles=30, source=source)


def test_a_search_records_the_parts_no_server_answered_for():
    """The day's record keeps which parts of the map data no server answered for (the
    Map page shades them): their boxes, none, all of it, or not known when stopped."""
    from leadgen.web import finding
    centre = config.SERVICE_CENTER
    box = (40.9, -112.3, 41.1, -112.0)
    done = RunResult([], centre, "AARCO", [], {})
    assert finding.map_areas(done, _params()) == {"missing": []}
    assert finding.map_areas(done, _params("google")) is None          # the map data wasn't asked
    missed = RunResult([], centre, "AARCO", [], {}, osm_missing=[(box, 1.0, 0)], osm_areas=9)
    assert finding.map_areas(missed, _params()) == {"missing": [list(box)]}
    down = RunResult([], centre, "AARCO", [], {}, failed_sources=["osm"])
    assert finding.map_areas(down, _params("osm")) == {"missing": [None]}
    stopped = RunResult([], centre, "AARCO", [], {}, stopped="paused")
    assert finding.map_areas(stopped, _params()) == {"known": False}
    assert osm.part_boxes([(None, 1.0, 0), (box, 1.0, 1)]) == [None, list(box)]


def test_a_search_records_where_it_ran_and_what_the_map_data_missed(monkeypatch):
    monkeypatch.setattr(web.finding, "run", lambda params, progress: RunResult(
        [_lead()], (40.76, -111.89), "Salt Lake City, UT", [], {},
        partial_sources=["osm"], osm_missing=[((40.9, -112.3, 41.1, -112.0), 1.0, 0)], osm_areas=9))
    client = web.create_app().test_client()
    _wait(client, client.post("/search", data={"location": "84101"}).get_json()["job_id"])
    record = daily.history()["searches"][0]
    assert record["center"] == [40.7559, -111.8967]                    # the place looked up for 84101
    assert record["map_areas"] == {"missing": [[40.9, -112.3, 41.1, -112.0]]}
