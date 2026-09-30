"""Closed businesses stay in the list, an incomplete search gives the day back, and
problems on the live site are recorded and sent to whoever looks after it."""

import csv
import io
import json
import logging
import time

import pytest

from leadgen import alerts, calls, config, daily, marks, pipeline, saved, store, web
from leadgen.models import Lead
from leadgen.scoring import score_lead
from leadgen.sources import SourceError


def _lead(name="Costco", sid="c", **kw):
    kw.setdefault("lat", 40.72)
    kw.setdefault("lon", -111.9)
    kw.setdefault("raw_categories", ["shop=wholesale"])
    lead = Lead(name=name, source="osm", source_id=sid, **kw)
    score_lead(lead, config.DEFAULT_KEYWORDS)
    return lead


def _wait(client, job):
    for _ in range(200):
        body = client.get(f"/status/{job}").get_json()
        if body["state"] != "running":
            return body
        time.sleep(0.05)
    raise AssertionError("the search never finished")


# ---- a business that closed for good keeps its row, mark and calls

def _closed_after_a_call():
    called, fresh = _lead("Costco", "c1"), _lead("Smith's", "c2", lat=40.75)
    saved.save_search([called, fresh])
    marks.set_mark(called.uid, "yes")
    calls.log_call(called.uid, "Follow Up", "Call back in May")
    saved.save_search([_lead("Costco", "c1", business_status="CLOSED_PERMANENTLY"),
                       _lead("Smith's", "c2", lat=40.75, business_status="CLOSED_PERMANENTLY")])
    return called, fresh


def test_a_closed_business_stays_in_every_view_it_belongs_to():
    called, fresh = _closed_after_a_call()
    client = web.create_app().test_client()
    body = client.get("/leads?tab=yes").get_json()
    [row] = body["leads"]
    assert row["key"] == called.uid and row["closed"] and row["has_baler"] == "yes"
    assert row["call_outcome"] == "Follow Up" and saved.CLOSED_FLAG in row["flags"]
    assert body["counts"] | {"outcomes": None} == {
        "unchecked": 0, "yes": 1, "no": 0, "competitors": 0, "closed": 2, "all": 2, "called": 1,
        "outcomes": None}
    closed = client.get("/leads?tab=closed").get_json()["leads"]
    assert {l["key"] for l in closed} == {called.uid, fresh.uid}
    assert client.get("/leads?tab=unchecked").get_json()["leads"] == []
    assert [l["key"] for l in client.get("/leads?tab=called").get_json()["leads"]] == [called.uid]
    assert client.get("/leads?tab=all&q=closed%20for%20good").get_json()["total"] == 2


def test_a_closed_business_is_in_the_downloads_and_still_counts_in_stats():
    called, _ = _closed_after_a_call()
    client = web.create_app().test_client()
    rows = list(csv.DictReader(io.StringIO(client.get("/download/saved.csv").data.decode("utf-8-sig"))))
    costco = next(r for r in rows if r["Business Name"] == "Costco")
    assert costco["Flags"].startswith(saved.CLOSED_FLAG)
    assert costco["Has Baler or Compactor?"] == "Yes" and costco["Call Result"] == "Follow Up"
    stats = client.get("/stats").get_json()
    assert stats["with_equipment"] == 1 and stats["checked"] == 1 and stats["closed"] == 1


def test_a_business_that_reopens_loses_the_flag():
    lead = _lead()
    saved.save_search([lead])
    saved.save_search([_lead(business_status="CLOSED_PERMANENTLY")])
    assert saved.is_closed(saved.load()[0])
    saved.save_search([_lead(business_status="OPERATIONAL")])
    [back] = saved.load()
    assert not saved.is_closed(back) and saved.CLOSED_FLAG not in back.flags


# ---- a search where one source failed gives the day back

def _google_refused(monkeypatch):
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "AIzaFAKEKEYFORTESTS000000000000000000")
    monkeypatch.setattr(pipeline, "geocode", lambda location, key: (40.72, -111.9, "Salt Lake City"))

    def refused(*args, **kw):
        raise SourceError("Google rejected the request. Check the API key. HTTP 403: denied")
    monkeypatch.setattr(pipeline.google_places, "search", refused)
    monkeypatch.setattr(pipeline.osm, "search", lambda *a, **k: ([_lead()], []))


def test_a_search_whose_paid_source_failed_saves_what_it_found_and_gives_the_day_back(monkeypatch):
    _google_refused(monkeypatch)
    monkeypatch.setattr(alerts, "ON", True)
    client = web.create_app().test_client()
    job = client.post("/search", data={"location": "84101"}).get_json()["job_id"]
    body = _wait(client, job)
    assert body["state"] == "done" and body["saved"] and body["found"] == 1
    text = " ".join(body["warnings"])
    assert "Couldn't reach Google" in text and "Today's search was not used up" in text
    assert "check the Google key" in text
    history = client.get("/searches").get_json()
    assert not history["used_today"] and history["current"] is None
    record = history["searches"][0]
    assert record["failed"] and record["partial"] and record["leads"] == 1 and record["new"] == 1
    assert "Couldn't reach Google" in record["reason"] and "were saved" in record["reason"]
    assert [l["name"] for l in client.get("/leads").get_json()["leads"]] == ["Costco"]
    assert history["problems"]["count"] == 1
    assert "incomplete" in history["problems"]["latest"][0]["text"]
    # The search can be run again today, and a complete one then uses the day up.
    monkeypatch.setattr(pipeline.google_places, "search", lambda *a, **k: ([], 1, []))
    job = client.post("/search", data={"location": "84101"}).get_json()["job_id"]
    assert _wait(client, job)["state"] == "done"
    assert client.get("/searches").get_json()["used_today"]
    assert client.post("/search", data={"location": "84101"}).status_code == 409


def test_the_pipeline_names_the_sources_that_failed(monkeypatch):
    _google_refused(monkeypatch)
    res = pipeline.run(pipeline.SearchParams())
    assert res.failed_sources == ["google"] and [l.name for l in res.leads] == ["Costco"]
    monkeypatch.setattr(pipeline.google_places, "search", lambda *a, **k: ([], 1, []))
    assert pipeline.run(pipeline.SearchParams()).failed_sources == []


def test_release_keeps_what_an_incomplete_search_found():
    day, _ = daily.claim({"location": "84101"})
    daily.release(day, "Couldn't reach Yelp", {"partial": True, "leads": 4})
    [record] = daily.history()["searches"]
    assert record["partial"] and record["leads"] == 4 and record["location"] == "84101"
    assert daily.claim({"location": "84101"})[0] == day


# ---- problems are recorded and sent to the webhook

@pytest.fixture
def reporting(monkeypatch):
    monkeypatch.setattr(alerts, "ON", True)
    sent = []

    class Answer:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b"ok"

    def fake_urlopen(request, timeout):
        sent.append((request.full_url, json.loads(request.data), timeout))
        return Answer()
    monkeypatch.setattr(alerts.urllib.request, "urlopen", fake_urlopen)
    return sent


def test_a_failed_search_is_recorded_and_sent_to_the_webhook(monkeypatch, reporting):
    monkeypatch.setenv(alerts.WEBHOOK_ENV, "https://hooks.example.com/abc")

    def unreachable(*args, **kw):
        raise SourceError("All OpenStreetMap (Overpass) servers failed: timeout")
    monkeypatch.setattr(pipeline.osm, "search", unreachable)
    client = web.create_app().test_client()
    job = client.post("/search", data={"location": "84101"}).get_json()["job_id"]
    assert _wait(client, job)["state"] == "error"
    [(url, body, timeout)] = reporting
    assert url == "https://hooks.example.com/abc" and timeout == 10
    assert body["text"] == body["content"]
    assert body["text"].startswith(f"{alerts.SITE}: Today's search failed: Couldn't reach the map")
    problems = client.get("/searches").get_json()["problems"]
    assert problems["count"] == 1 and problems["latest"][0]["kind"] == "search"


def test_logged_errors_are_reported_once_in_a_while(monkeypatch, reporting):
    monkeypatch.setenv(alerts.WEBHOOK_ENV, "https://hooks.example.com/abc")
    alerts.install()
    alerts.install()                                   # once, however often it is called
    handlers = [h for h in logging.getLogger("leadgen").handlers if isinstance(h, alerts._Handler)]
    assert len(handlers) == 1
    log = logging.getLogger("leadgen.web")
    try:
        raise ConnectionError("secret detail")
    except ConnectionError:
        log.error("Loading the saved leads failed", exc_info=True)
        log.error("Loading the saved leads failed", exc_info=True)   # the same again: throttled
    logging.getLogger("leadgen.web").warning("Only a warning")        # not a problem
    assert [b["text"].split(" (")[0] for _, b, _ in reporting] == [
        f"{alerts.SITE}: Loading the saved leads failed"]
    assert "ConnectionError" in reporting[0][1]["text"] and "secret" not in reporting[0][1]["text"]
    assert alerts.recent()["count"] == 1


def test_a_webhook_that_fails_or_is_not_https_breaks_nothing(monkeypatch, reporting, caplog):
    monkeypatch.setenv(alerts.WEBHOOK_ENV, "http://hooks.example.com/plain")
    with caplog.at_level(logging.WARNING):
        alerts.report("error", "Something broke")
    assert reporting == [] and "Sending the problem" in caplog.text
    assert alerts.recent()["count"] == 1               # still recorded for the page


def test_without_a_database_the_report_is_only_logged(monkeypatch, reporting, caplog):
    def down():
        raise store.Unavailable("the database could not be reached")
    monkeypatch.setattr(store, "open_db", down)
    with caplog.at_level(logging.WARNING):
        alerts.report("error", "Loading the saved leads failed")
    assert "Recording a problem failed: Unavailable" in caplog.text


def test_problems_older_than_a_week_are_not_shown(reporting):
    alerts.report("error", "Old")
    alerts.report("error", "New")
    later = time.time() + alerts.RECENT_SECONDS - 60
    assert alerts.recent()["count"] == 2
    assert alerts.recent(now=later + 120)["count"] == 0
