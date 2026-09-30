"""The day's one search: a search whose leads can't be saved gives the day back, an
incomplete search uses up the day (unless re-runs are allowed), and shows one short note."""

import time

from leadgen import config, daily, pipeline, saved, store, web
from leadgen.models import Lead
from leadgen.pipeline import RunResult
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


# ---- an incomplete search may be run again a set number of times

def test_incomplete_searches_can_be_rerun_once_when_allowed(monkeypatch):
    """INCOMPLETE_RERUNS is 0 (the owner's one-search-a-day rule); raised to 1, an
    incomplete search may be run once more that day."""
    monkeypatch.setattr(daily, "INCOMPLETE_RERUNS", 1)
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "AIzaFAKEKEYFORTESTS000000000000000000")
    monkeypatch.setattr(pipeline, "geocode", lambda location, key: (40.72, -111.9, "Salt Lake City"))

    def refused(*args, **kw):
        raise SourceError("Google rejected the request. HTTP 403: denied")
    monkeypatch.setattr(pipeline.google_places, "search", refused)
    monkeypatch.setattr(pipeline.osm, "search", lambda *a, **k: ([_lead()], []))
    client = web.create_app().test_client()
    job = client.post("/search", data={"location": "84101"}).get_json()["job_id"]
    assert "run it again once today" in " ".join(_wait(client, job)["warnings"])
    history = client.get("/searches").get_json()
    assert not history["used_today"] and history["reruns_left"] == 1
    # The re-run is incomplete too: it keeps the day.
    job = client.post("/search", data={"location": "84101"}).get_json()["job_id"]
    assert "today's search is now used up" in " ".join(_wait(client, job)["warnings"])
    history = client.get("/searches").get_json()
    assert history["used_today"] and history["current"]["partial"]
    third = client.post("/search", data={"location": "84101"})
    assert third.status_code == 409
    assert "the next search can run tomorrow, from midnight Utah time" in third.get_json()["error"]
    assert daily.incomplete_count(daily.today()) == 1


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
