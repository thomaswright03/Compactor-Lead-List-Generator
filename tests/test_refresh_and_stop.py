"""Keeping an open page up to date cheaply, the stop switches on a running search, and
the plain words on the Find leads page."""

import json
import re
import time
from pathlib import Path

import pytest

from leadgen import calls, config, geo, marks, pipeline, saved, store, web
from leadgen.models import Lead
from leadgen.pipeline import PipelineError, SearchParams
from leadgen.scoring import score_lead
from leadgen.sources import google_places, osm

FIX = Path(__file__).parent / "fixtures"


def _lead(name, sid, lat=40.72, lon=-111.9):
    lead = Lead(name=name, lat=lat, lon=lon, source="osm", source_id=sid,
                raw_categories=["shop=wholesale"])
    score_lead(lead, config.DEFAULT_KEYWORDS)
    return lead


def _saved(n):
    leads = [_lead(f"Business {i}", f"n{i}", lat=40.3 + (i // 80) * 0.01,
                   lon=-112.2 + (i % 80) * 0.01) for i in range(n)]
    saved.save_search(leads, config.DEFAULT_KEYWORDS)
    return leads


# ---- /leads?since: only what changed

def test_refresh_sends_only_what_changed(monkeypatch):
    monkeypatch.setattr(web.leads, "SINCE_OVERLAP", 0.0)
    a, b, c = _saved(3)
    client = web.create_app().test_client()
    first = client.get("/leads").get_json()
    assert len(first["leads"]) == 3
    since = first["now"]
    time.sleep(0.01)
    assert client.get(f"/leads?since={since}").get_json()["leads"] == []
    marks.set_mark(a.uid, "yes")                         # a colleague marks one
    calls.log_call(b.uid, "Interested", "Wants a quote")  # and logs a call on another
    body = client.get(f"/leads?since={since}").get_json()
    changed = {lead["name"]: lead for lead in body["leads"]}
    assert set(changed) == {a.name, b.name} and body["removed"] == []
    assert changed[a.name]["has_baler"] == "yes" and changed[a.name]["undo_mark"]
    assert changed[b.name]["call_outcome"] == "Interested"
    since = body["now"]
    time.sleep(0.01)
    assert calls.undo(changed[b.name]["undo_call"]["id"])  # an undone call shows up too
    body = client.get(f"/leads?since={since}").get_json()
    assert [(l["name"], l["call_count"]) for l in body["leads"]] == [(b.name, 0)]


def test_a_lead_that_closed_moves_to_the_closed_tab_on_refresh(monkeypatch):
    monkeypatch.setattr(web.leads, "SINCE_OVERLAP", 0.0)
    lead = _saved(1)[0]
    client = web.create_app().test_client()
    since = client.get("/leads?tab=unchecked").get_json()["now"]
    time.sleep(0.01)
    closed = _lead(lead.name, lead.source_id)
    closed.business_status = "CLOSED_PERMANENTLY"
    saved.save_search([closed])
    body = client.get(f"/leads?tab=unchecked&since={since}").get_json()
    [changed] = body["leads"]
    assert changed["key"] == lead.uid and changed["closed"] and not changed["in_view"]
    assert body["removed"] == [] and body["counts"]["unchecked"] == 0
    assert body["counts"]["closed"] == 1 and body["counts"]["all"] == 1


def test_refresh_of_a_big_list_is_small_and_quick(monkeypatch):
    monkeypatch.setattr(web.leads, "SINCE_OVERLAP", 0.0)
    _saved(5000)
    client = web.create_app().test_client()
    full = client.get("/leads")
    assert len(full.get_json()["leads"]) == 5000
    time.sleep(0.01)
    start = time.perf_counter()
    res = client.get(f"/leads?since={full.get_json()['now']}")     # an idle open page
    took = time.perf_counter() - start
    assert res.get_json()["leads"] == [] and len(res.data) < 200
    assert len(full.data) > 1000 * len(res.data)
    assert took < 0.5, took                        # a few ms locally; generous for CI


def test_marks_are_read_for_the_businesses_asked_about():
    leads = _saved(3)
    marks.set_mark(leads[0].uid, "no")
    seen = []
    real = store.Db.all

    def spy(self, sql, params=()):
        seen.append(sql)
        return real(self, sql, params)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(store.Db, "all", spy)
        assert marks.get_all([leads[0].uid]) == {leads[0].uid: "no"}
    assert all("WHERE uid IN" in sql for sql in seen)


# ---- the off switches stop a search that is already running

def test_pausing_stops_a_running_search_between_sources(monkeypatch):
    gdata = json.loads((FIX / "google_page.json").read_text())

    def google(*args, **kw):
        monkeypatch.setenv(config.SEARCH_PAUSED_ENV, "1")      # switched on mid-search
        return [google_places.parse_place(p, "q") for p in gdata["places"]], 1, []
    osm_called = []
    monkeypatch.setattr(google_places, "search", google)
    monkeypatch.setattr(osm, "search", lambda *a, **k: osm_called.append(1) or ([], []))
    res = pipeline.run(SearchParams(source="both", api_key="KEY", min_score=0))
    assert not osm_called and res.leads                      # what Google found is kept
    assert any("stopped by the administrator" in w for w in res.warnings)


def test_pausing_before_anything_was_found_fails_plainly(monkeypatch):
    def geocode(*args, **kw):
        monkeypatch.setenv(config.SEARCH_PAUSED_ENV, "1")      # switched on while locating
        return 40.76, -111.89, "SLC"
    monkeypatch.setattr(pipeline, "geocode", geocode)
    with pytest.raises(PipelineError, match="stopped by the administrator"):
        pipeline.run(SearchParams(source="osm"))


def test_switching_google_off_stops_before_the_next_paid_call(monkeypatch):
    sent = []

    def fake(method, url, **kw):
        sent.append(kw["json_body"]["textQuery"])
        monkeypatch.setenv(config.GOOGLE_OFF_ENV, "1")         # switched off after the first call
        return {"places": [{"id": f"p{len(sent)}", "displayName": {"text": "Store"},
                            "location": {"latitude": 40.76, "longitude": -111.89},
                            "types": ["store"]}]}
    monkeypatch.setattr(google_places, "request_json", fake)
    leads, n, warnings = google_places.search(40.76, -111.89, 30, ["a", "b", "c"], "KEY")
    assert n == 1 and len(leads) == 1
    assert web.plain_warning(warnings[-1]) == ("Google was stopped partway by the administrator; "
                                               "the businesses already found were kept.")


# ---- plain words on the Find leads page

def test_progress_and_details_are_in_plain_words():
    job = {}
    progress = web._Progress(job)
    osm_note = []
    progress("OpenStreetMap: searching the free map data (server 2 of 4)")
    osm_note.append(job["message"])
    progress("Yelp page 1: 'grocery' (3/19)")
    osm_note.append(job["message"])
    assert osm_note == ["Searching the free map data (server 2 of 4)…",
                        "Searching Yelp for grocery (3 of 19)"]
    # An older record (no funnel numbers): labelled and put in funnel order all the same.
    details = web.plain_details({"seconds": 8.2, "osm raw results": 40, "after dedupe": 30,
                                 "results in radius": 35, "google requests": 3})
    assert details == [["Businesses from the free map data", 40], ["Listings within the radius", 35],
                       ["Businesses after merging duplicates", 30], ["Google lookups", 3],
                       ["Took", "8 seconds"]]


def test_an_unknown_place_reads_plainly(monkeypatch):
    monkeypatch.setattr(geo, "request_json", lambda *a, **k: [])
    with pytest.raises(geo.GeocodeError) as err:
        geo.geocode("Nowhereville zz", "")
    assert str(err.value) == ("Could not find the place 'Nowhereville zz'. Try a 5-digit ZIP "
                              "code or a city name.")


def test_osm_progress_names_no_server(monkeypatch):
    said = []
    monkeypatch.setattr(osm, "request_json", lambda *a, **k: {"elements": []})
    osm.search(40.76, -111.89, 5, progress=said.append)
    assert said == ["OpenStreetMap: searching the free map data (server 1 of 4)"]


def test_interrupted_search_says_when_in_utah_time(monkeypatch):
    from leadgen import daily
    daily.claim({"location": "84101"})
    body = web.create_app().test_client().get("/searches").get_json()
    assert re.fullmatch(r"\d{1,2}:\d\d [AP]M", body["current"]["free_at"])


def test_a_failed_mark_says_so_once(monkeypatch):
    lead = _saved(1)[0]
    monkeypatch.setattr(marks, "set_mark", lambda *a: (_ for _ in ()).throw(store.Unavailable("x")))
    res = web.create_app().test_client().post("/mark", json={"key": lead.uid, "value": "yes"})
    assert res.status_code == 503
    assert "wasn't saved" not in res.get_json()["error"]         # the page says that part
