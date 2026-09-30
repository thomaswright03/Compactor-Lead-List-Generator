"""Round 7: a search whose leads can't be saved gives the day back, the map progress only
moves forward, the tests stay offline, who made each mark and call, building labels
left out, and form messages in the form's own words."""

import csv
import io
import socket
import time

import pytest

from leadgen import calls, config, daily, marks, pipeline, saved, store, web
from leadgen.export import to_csv_bytes
from leadgen.models import Lead
from leadgen.pipeline import RunResult
from leadgen.scoring import score_lead
from leadgen.sources import osm


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


# ---- the map data's progress only moves forward

def test_map_progress_never_counts_backwards():
    job = {}
    progress = web._Progress(job)
    seen = []
    for msg in ["(8 of 17 areas done)", "(7 of 17 areas done)",
                "(9 of 21 areas done, some areas asked again in smaller parts)",
                "(10 of 25 areas done, some areas asked again in smaller parts)",
                "(9 of 21 areas done, some areas asked again in smaller parts)"]:
        progress(f"OpenStreetMap: searching the free map data {msg}")
        seen.append(job["message"])
    done = [int(m.split(": ")[1].split(" of ")[0]) for m in seen]
    assert done == sorted(done) == [8, 8, 9, 10, 10]
    assert seen[0] == "Searching the free map data: 8 of 17 areas done…"
    assert seen[-1] == ("Searching the free map data: 10 of 25 areas done (some areas are being "
                        "asked again in smaller parts)…")


def test_the_map_search_reports_parts_done(monkeypatch):
    monkeypatch.setattr(osm, "request_json", lambda *a, **k: {"elements": []})
    said = []
    osm.search(40.76, -111.89, 30, progress=said.append)
    counts = [m for m in said if "areas done" in m]
    assert counts[0].endswith("(0 of 9 areas done)") and counts[-1].endswith("(9 of 9 areas done)")
    numbers = [int(m.rsplit("(", 1)[1].split()[0]) for m in counts]
    assert numbers == sorted(numbers)


# ---- the tests never reach the internet or write where they are run

def test_the_tests_cannot_reach_the_internet(tmp_path):
    with socket.socket() as sock, pytest.raises(OSError, match="may not reach the network"):
        sock.connect(("93.184.215.14", 443))
    import leadgen.http
    assert str(leadgen.http.CACHE_DIR) != ".cache"


# ---- who made each mark and call

def test_marks_and_calls_record_who_made_them():
    a, b = _lead(), _lead("Walmart", "w")
    saved.save_search([a, b])
    client = web.create_app().test_client()
    assert client.post("/mark", json={"key": a.uid, "value": "yes", "by": " Dana\n  Smith "}).status_code == 200
    assert client.post("/mark", json={"key": b.uid, "value": "no", "by": "Lee"}).status_code == 200
    call = client.post("/calls", json={"key": a.uid, "outcome": "Follow Up", "notes": "Quote",
                                       "by": "Lee"}).get_json()["call"]
    assert call["by"] == "Lee"
    rows = {l["name"]: l for l in client.get("/leads").get_json()["leads"]}
    assert rows["Costco"]["marked_by"] == "Dana Smith" and rows["Walmart"]["marked_by"] == "Lee"
    assert rows["Costco"]["last_call_by"] == "Lee"
    assert [c["by"] for c in client.get(f"/calls/{a.uid}").get_json()["calls"]] == ["Lee"]
    file = list(csv.DictReader(io.StringIO(to_csv_bytes(
        calls.apply(marks.apply(saved.load()))).decode("utf-8-sig"))))
    by_name = {r["Business Name"]: r for r in file}
    assert by_name["Costco"]["Marked By"] == "Dana Smith" and by_name["Costco"]["Called By"] == "Lee"
    assert by_name["Walmart"]["Called By"] == ""


def _maker(uid):
    [lead] = marks.apply(saved.load([uid]))
    return lead.marked_by


def test_a_mark_switched_by_someone_else_names_them_and_undo_names_the_first():
    lead = _lead()
    saved.save_search([lead])
    marks.set_mark(lead.uid, "yes", "Dana")
    undo = marks.set_mark(lead.uid, "no", "Lee")
    assert _maker(lead.uid) == "Lee"
    assert marks.undo(undo["id"])
    assert _maker(lead.uid) == "Dana"
    # A click with no name (or from before names were kept) names nobody.
    marks.set_mark(lead.uid, "no")
    assert _maker(lead.uid) == ""
    assert store.person("x" * 100) == "x" * store.MAX_NAME and store.person(None) == ""


def test_a_call_sent_twice_keeps_its_maker():
    lead = _lead()
    saved.save_search([lead])
    first = calls.log_call(lead.uid, "Interested", "", "a" * 32, "Dana")
    again = calls.log_call(lead.uid, "Interested", "", "a" * 32, "Someone else")
    assert first["by"] == again["by"] == "Dana"


# ---- building labels are left out; OSM-only searches say phones will be few

@pytest.mark.parametrize("name", ["Building B", "Bldg. 3", "BUILDING 12A", "Tower 2", "C Building",
                                  "Suite 100", "Wing C"])
def test_labels_for_part_of_a_complex_are_left_out(name):
    element = {"type": "way", "id": 5, "bounds": {"minlat": 40.7, "maxlat": 40.71,
                                                  "minlon": -111.9, "maxlon": -111.89},
               "tags": {"name": name, "building": "apartments"}}
    assert osm.parse_element(element) is None


@pytest.mark.parametrize("name", ["Tower Records", "Building Supply Co", "Suite Dreams Bakery",
                                  "Harmons", "3M", "The Block Restaurant"])
def test_real_business_names_are_kept(name):
    element = {"type": "node", "id": 6, "lat": 40.7, "lon": -111.9,
               "tags": {"name": name, "shop": "supermarket"}}
    assert osm.parse_element(element).name == name


def test_find_leads_says_phones_will_be_few_without_google_or_yelp(monkeypatch):
    page = web.create_app().test_client().get("/").get_data(as_text=True)
    assert "most businesses found will have no phone number" in page
    monkeypatch.setenv("YELP_API_KEY", "fake-yelp-key-for-tests")
    page = web.create_app().test_client().get("/").get_data(as_text=True)
    assert "most businesses found will have no phone number" not in page


# ---- form messages use the form's own words

def test_form_messages_use_the_labels_on_screen():
    client = web.create_app().test_client()
    body = client.post("/search", data={"location": "84101", "radius": "0"}).get_json()
    assert body == {"error": "How far must be between 1 and 100 miles", "field": "radius"}
    body = client.post("/search", data={"location": "84101", "min_score": "200"}).get_json()
    assert body["error"].startswith("The score to leave out weak leads below must be between 0 and 100")
    assert not daily.history()["used_today"]


def test_an_unknown_place_gives_the_same_advice_as_the_hint(monkeypatch):
    from leadgen import geo
    monkeypatch.setattr(geo, "request_json", lambda *a, **k: [])
    with pytest.raises(geo.GeocodeError, match="try a ZIP code, city or street address"):
        geo.geocode("Nowhereville zz", "")
    assert pipeline.geocode is geo.geocode
