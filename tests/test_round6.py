"""The map data asked in parts, closed businesses never added, small refreshes after a
search, calls logged on any prospect, and a limit on re-running incomplete searches."""

import csv
import io
import re
import time

import pytest

from leadgen import calls, config, daily, marks, pipeline, saved, web
from leadgen.http import HttpError
from leadgen.models import Lead
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


# ---- a closed business nobody saved is never added

def test_a_new_closed_business_is_not_added_to_the_saved_list():
    open_one = _lead("Smith's", "s1", lat=40.75)
    closed = _lead("Old Mill Foods", "m1", business_status="CLOSED_PERMANENTLY")
    new, updated = saved.save_search([open_one, closed])
    assert (new, updated) == (1, 0)
    assert [l.name for l in saved.load()] == ["Smith's"]
    assert closed.uid == "" and open_one.uid
    # Found open later, it is added like any new business.
    saved.save_search([_lead("Old Mill Foods", "m1")])
    assert {l.name for l in saved.load()} == {"Smith's", "Old Mill Foods"}


def test_a_saved_business_that_closes_keeps_its_row():
    lead = _lead()
    saved.save_search([lead])
    saved.save_search([_lead(business_status="CLOSED_PERMANENTLY")])
    [row] = saved.load()
    assert row.uid == lead.uid and saved.CLOSED_FLAG in row.flags


# ---- refreshing after a search stays small, and the parsed list is reused

def _many(n):
    leads = [_lead(f"Biz {i}", f"n{i}", lat=40.5 + (i % 50) * 0.01, lon=-112 + (i // 50) * 0.01,
                   raw_categories=["shop=supermarket"]) for i in range(n)]
    saved.save_search(leads, config.DEFAULT_KEYWORDS)
    return leads


def test_a_refresh_after_a_big_search_sends_one_page_at_most(monkeypatch):
    monkeypatch.setattr(web.leads, "SINCE_OVERLAP", 0.0)
    leads = _many(1500)
    client = web.create_app().test_client()
    since = client.get("/leads?tab=unchecked&limit=300").get_json()["now"]
    time.sleep(0.01)
    saved.save_search(leads, config.DEFAULT_KEYWORDS)          # the search touches every lead
    res = client.get(f"/leads?since={since}&tab=unchecked&limit=300")
    body = res.get_json()
    assert body["reload"] and body["leads"] == [] and body["counts"]["unchecked"] == 1500
    assert len(res.data) < 20_000
    # A few changes come back as rows: in full when in the view, just the key otherwise.
    since = body["now"]
    time.sleep(0.01)
    marks.set_mark(leads[0].uid, "yes")
    body = client.get(f"/leads?since={since}&tab=unchecked&limit=300").get_json()
    assert body.get("reload") is None
    assert body["leads"] == [{"key": leads[0].uid, "in_view": False}]
    [row] = client.get(f"/leads?since={since}&tab=yes&limit=300").get_json()["leads"]
    assert row["key"] == leads[0].uid and row["in_view"] and row["has_baler"] == "yes"


def test_the_parsed_list_is_reused_until_a_search_changes_it(monkeypatch):
    _many(20)
    parsed = []
    real = saved._to_lead
    monkeypatch.setattr(saved, "_to_lead", lambda data: parsed.append(1) or real(data))
    first = saved.load()
    assert len(parsed) == 20
    again = saved.load()
    assert len(parsed) == 20 and [l.uid for l in again] == [l.uid for l in first]
    again[0].has_baler = "yes"                     # each caller gets its own copies
    assert saved.load()[0].has_baler == ""
    saved.save_search([_lead("Harmons", "h1", raw_categories=["shop=supermarket"])])
    parsed.clear()
    assert len(saved.load()) == 21 and len(parsed) == 21     # parsed again after the change


# ---- calls can be logged on any prospect, whatever its mark

def test_a_call_on_an_unchecked_business_keeps_it_unchecked():
    lead = _lead()
    saved.save_search([lead])
    client = web.create_app().test_client()
    assert client.post("/calls", json={"key": lead.uid, "outcome": "No Contact",
                                       "notes": "Rang twice"}).status_code == 200
    unchecked = client.get("/leads?tab=unchecked").get_json()
    assert [l["key"] for l in unchecked["leads"]] == [lead.uid]
    assert unchecked["leads"][0]["call_outcome"] == "No Contact"
    called = client.get("/leads?tab=called").get_json()
    assert [l["key"] for l in called["leads"]] == [lead.uid]
    assert called["counts"]["outcomes"]["No Contact"] == 1
    rows = list(csv.DictReader(io.StringIO(client.get("/download/saved.csv").data.decode("utf-8-sig"))))
    assert rows[0]["Call Result"] == "No Contact" and rows[0]["Has Baler or Compactor?"] == ""
    assert marks.get_all([lead.uid]) == {} and len(calls.history(lead.uid)) == 1


# ---- the map data is asked in parts

def _box_of(query):
    m = re.search(r"\(around:[^)]*\)\(([-\d.]+),([-\d.]+),([-\d.]+),([-\d.]+)\)", query)
    return tuple(map(float, m.groups())) if m else None


def _place(n, lat, lon):
    return {"type": "node", "id": n, "lat": lat, "lon": lon,
            "tags": {"name": f"Market {n}", "shop": "supermarket"}}


def test_a_wide_search_is_asked_in_parts_and_a_big_query_is_never_needed(monkeypatch):
    """Every query covering more than one part's size fails (as busy public servers
    do); the 30-mile search still finds the businesses in every part."""
    asked = []

    def servers(method, url, data, **kw):
        box = _box_of(data["data"])
        asked.append(box)
        if box is None or box[2] - box[0] > 0.3:
            raise HttpError(f"{url} returned HTTP 504")
        lat, lon = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        return {"elements": [_place(int(abs(lat * 1e4) + abs(lon * 1e4)), lat, lon)]}
    monkeypatch.setattr(osm, "request_json", servers)
    leads, warnings = osm.search(40.76, -111.89, 30)
    assert len(asked) == 9 and None not in asked and warnings == []
    assert len({l.source_id for l in leads}) == 9


def test_a_failed_part_is_asked_again_in_quarters(monkeypatch):
    first = []

    def servers(method, url, data, **kw):
        box = _box_of(data["data"])
        if box and not first:
            first.append(box)
        if box == first[0]:
            raise HttpError(f"{url} returned HTTP 504")
        lat, lon = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        return {"elements": [_place(int(lat * 1e5) + int(-lon * 1e5), lat, lon)]}
    monkeypatch.setattr(osm, "request_json", servers)
    monkeypatch.setattr(config, "OVERPASS_ENDPOINTS", config.OVERPASS_ENDPOINTS[:1])
    leads, _ = osm.search(40.76, -111.89, 30)
    assert len(leads) == 8 + 4                     # the other 8 parts, and the failed one's quarters


def test_parts_no_server_answers_leave_the_map_data_incomplete(monkeypatch):
    monkeypatch.setattr(config, "OVERPASS_ENDPOINTS", config.OVERPASS_ENDPOINTS[:1])

    def servers(method, url, data, **kw):
        box = _box_of(data["data"])
        if box[0] > 40.8:                           # the northern parts never answer
            raise HttpError(f"{url}: ReadTimeout")
        lat, lon = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        return {"elements": [_place(int(lat * 1e5) + int(-lon * 1e5), lat, lon)]}
    monkeypatch.setattr(osm, "request_json", servers)
    with pytest.raises(osm.PartialResult) as got:
        osm.search(40.76, -111.89, 30)
    assert len(got.value.leads) == 6                # the southern and middle rows of parts
    assert "part of the area (about 33% missing)" in str(got.value)


def test_a_partial_map_result_is_saved_and_the_search_marked_incomplete(monkeypatch):
    monkeypatch.setattr(pipeline, "geocode", lambda location, key: (40.72, -111.9, "Salt Lake City"))

    def partial(*a, **k):
        raise osm.PartialResult("OpenStreetMap answered for only part of the area",
                                [_lead()], [])
    monkeypatch.setattr(pipeline.osm, "search", partial)
    client = web.create_app().test_client()
    job = client.post("/search", data={"location": "84101"}).get_json()["job_id"]
    body = _wait(client, job)
    assert body["state"] == "done" and body["saved"] and body["found"] == 1
    assert "answered for only part of the area" in " ".join(body["warnings"])
    record = client.get("/searches").get_json()["searches"][0]
    assert record["partial"] and "only part of the area" in record["reason"]
    assert [l["name"] for l in client.get("/leads").get_json()["leads"]] == ["Costco"]


def test_progress_says_how_many_parts_of_the_map_are_done():
    job = {}
    progress = web._Progress(job)
    progress("OpenStreetMap: searching the free map data (3 of 9 areas done)")
    assert job["message"] == "Searching the free map data: 3 of 9 areas done…"
    assert job["step"] == 2 and job["pct"] == pytest.approx(62, abs=1)


# ---- an incomplete search may be run again a set number of times

def test_incomplete_searches_can_be_rerun_once(monkeypatch):
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
