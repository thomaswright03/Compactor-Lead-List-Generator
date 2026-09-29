"""The saved lead list and its Yes / No "has a baler" marks."""

import time

from leadgen import config, marks, saved, web
from leadgen.dedupe import dedupe
from leadgen.export import COLUMNS, to_csv_bytes
from leadgen.models import Lead
from leadgen.pipeline import RunResult


def _lead(name="Smith's Marketplace", source="yelp", source_id="y1", **kw):
    base = dict(lat=40.75, lon=-111.87, phone="(801) 555-0100", zip="84102", score=70,
                tier="A", raw_categories=["yelp:grocery"], yelp_reviews=100)
    base.update(kw)
    return Lead(name=name, source=source, source_id=source_id, **base)


def test_searches_merge_into_one_saved_row_and_keep_the_mark():
    first = _lead()
    assert saved.save_search([first], []) == (1, 0)
    marks.set_mark(first.uid, "yes")
    # A later search finds it again: under a new listing id, with Google too.
    again = dedupe([_lead(source_id="y2", yelp_reviews=150),
                    _lead(name="Smiths Marketplace", source="google", source_id="g1",
                          raw_categories=["grocery_store"], lat=40.7502)])[0]
    other = _lead(name="Costco", source_id="y3", phone="(801) 555-0199", lat=40.70)
    assert saved.save_search([again, other], []) == (1, 1)
    assert again.uid == first.uid != other.uid
    leads = saved.load()
    assert len(leads) == 2
    smiths = next(l for l in leads if l.uid == first.uid)
    assert smiths.has_baler == "yes" and sorted(smiths.sources) == ["google", "yelp"]
    assert smiths.yelp_reviews == 150
    assert smiths.distance_miles is not None


def test_yelp_details_expire_but_the_mark_comes_back(monkeypatch):
    lead = _lead()
    saved.save_search([lead], [])
    marks.set_mark(lead.uid, "no")
    osm = _lead(name="Walmart", source="osm", source_id="node/1", phone="", lat=40.6,
                raw_categories=["shop=supermarket"], yelp_reviews=None)
    saved.save_search([osm], [])
    later = time.time() + config.SAVED_SOURCE_KEEP_SECONDS["yelp"] + 60
    monkeypatch.setattr(saved.time, "time", lambda: later)
    assert [l.name for l in saved.load()] == ["Walmart"]        # map data is kept
    again = _lead(source_id="y1")                                 # the same Yelp listing
    saved.save_search([again], [])
    assert again.uid == lead.uid
    assert {l.name: l.has_baler for l in saved.load()} == {"Walmart": "", lead.name: "no"}


def test_merged_lead_drops_only_the_yelp_part(monkeypatch):
    both = dedupe([_lead(), _lead(source="osm", source_id="way/9", phone="", footprint_sqft=50000,
                                 raw_categories=["shop=supermarket"], yelp_reviews=None)])[0]
    assert len(both.parts) == 2
    saved.save_search([both], [])
    later = time.time() + config.SAVED_SOURCE_KEEP_SECONDS["yelp"] + 60
    monkeypatch.setattr(saved.time, "time", lambda: later)
    lead = saved.load()[0]
    assert lead.sources == ["osm"] and lead.yelp_reviews is None and not lead.phone
    assert lead.footprint_sqft == 50000 and lead.uid == both.uid


def test_marks_saved_cleared_and_exported():
    a, b = _lead(), _lead(name="Costco", source_id="y3", phone="(801) 555-0199", lat=40.70)
    saved.save_search([a, b], [])
    marks.set_mark(a.uid, "yes")
    marks.set_mark(b.uid, "no")
    leads = saved.load()
    assert "Has Baler?" in [c for c, _, _ in COLUMNS]
    csv = to_csv_bytes(leads).decode("utf-8-sig")
    assert ",Yes," in csv and ",No," in csv
    marks.set_mark(a.uid, "")
    assert {l.name: l.has_baler for l in saved.load()}[a.name] == ""


def _search(client):
    job = client.post("/search", data={"location": "84101"}).get_json()["job_id"]
    for _ in range(100):
        body = client.get(f"/status/{job}").get_json()
        if body["state"] != "running":
            return job, body
        time.sleep(0.05)


def test_page_shows_saved_leads_across_searches(monkeypatch):
    found = iter([[_lead(name="Walmart", source_id="w1")],
                  [_lead(name="Walmart", source_id="w1"), _lead(name="Costco", source_id="c1",
                                                                phone="", lat=40.70)]])
    monkeypatch.setattr(web, "run", lambda params, progress: RunResult(
        next(found), (40.76, -111.89), "SLC", [], {}))
    client = web.create_app().test_client()
    assert client.get("/leads").get_json() == {"leads": []}
    _, body = _search(client)
    row = body["leads"][0]
    assert body["saved"] and row["has_baler"] == "" and row["key"]
    assert client.post("/mark", json={"key": row["key"], "value": "yes"}).get_json() == {"ok": True}
    _, body = _search(client)
    assert body["stats"]["new leads saved"] == 1 and body["stats"]["saved leads"] == 2
    assert {l["name"]: l["has_baler"] for l in body["leads"]} == {"Walmart": "yes", "Costco": ""}
    assert len(client.get("/leads").get_json()["leads"]) == 2
    csv = client.get("/download/saved.csv").data.decode("utf-8-sig")
    assert "Walmart" in csv and "Costco" in csv and ",Yes," in csv


def test_without_a_database_on_render_searches_still_show(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setattr(web, "run", lambda params, progress: RunResult(
        [_lead(name="Walmart")], (40.76, -111.89), "SLC", [], {}))
    client = web.create_app().test_client()
    assert "DATABASE_URL" in client.get("/leads").get_json()["error"]
    job, body = _search(client)
    assert not body["saved"] and body["leads"][0]["name"] == "Walmart"
    assert any("not saved" in w for w in body["warnings"])
    assert client.get(f"/download/{job}.csv").status_code == 200
    res = client.post("/mark", json={"key": "abc", "value": "yes"})
    assert res.status_code == 503 and "DATABASE_URL" in res.get_json()["error"]


def test_mark_endpoint_checks_input():
    client = web.create_app().test_client()
    assert client.post("/mark", json={"key": "k", "value": "maybe"}).status_code == 400
    assert client.post("/mark", json={"value": "yes"}).status_code == 400
    assert client.post("/mark", json={"key": "k", "value": "yes"},
                       headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
