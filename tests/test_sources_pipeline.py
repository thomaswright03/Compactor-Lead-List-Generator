import io
import json
from pathlib import Path

import pytest
from openpyxl import load_workbook

from leadgen import pipeline
from leadgen.export import format_phone, to_csv_bytes, to_xlsx_bytes
from leadgen.pipeline import PipelineError, SearchParams
from leadgen.sources import google_places, osm

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def no_cache(tmp_path, monkeypatch):
    monkeypatch.setattr("leadgen.http.CACHE_DIR", tmp_path / "cache")
    monkeypatch.delenv("GOOGLE_PLACES_API_KEY", raising=False)


def test_parse_google_place():
    data = json.loads((FIX / "google_page.json").read_text())
    lead = google_places.parse_place(data["places"][0], "supermarket")
    assert lead.name == "Smith's Marketplace"
    assert (lead.address, lead.city, lead.state, lead.zip) == ("455 S 500 E", "Salt Lake City", "UT", "84102")
    assert lead.rating_count == 2450 and lead.search_terms == ["supermarket"]


def test_google_search_paginates_and_respects_cap(monkeypatch):
    page = json.loads((FIX / "google_page.json").read_text())
    calls = []

    def fake(method, url, **kw):
        calls.append(kw["json_body"])
        return page if len(calls) % 3 else {"places": []}

    monkeypatch.setattr(google_places, "request_json", fake)
    leads, n, warnings = google_places.search(40.76, -111.89, 30, ["a", "b"], "KEY",
                                              grid_cells=1, max_requests=4)
    assert n == 4 and warnings  # hit the cap
    assert calls[1]["pageToken"] == "abc"
    assert calls[0]["locationBias"]["circle"]["radius"] == 48280.32


def test_osm_query_and_parse():
    q = osm.build_query(40.76, -111.89, 30, ["baler"])
    assert "around:48280" in q and "baler" in q and "out tags bb" in q
    data = json.loads((FIX / "overpass.json").read_text())
    leads = [l for l in map(osm.parse_element, data["elements"]) if l]
    assert len(leads) == 4  # unnamed node dropped
    wh = next(l for l in leads if l.name == "Acme Distribution Center")
    assert wh.footprint_sqft > 100_000 and "building=warehouse" in wh.raw_categories
    assert "brand=Smith's" in leads[0].raw_categories


def _fake_sources(monkeypatch):
    gdata = json.loads((FIX / "google_page.json").read_text())
    odata = json.loads((FIX / "overpass.json").read_text())
    monkeypatch.setattr(google_places, "search", lambda *a, **k: (
        [google_places.parse_place(p, "q") for p in gdata["places"]], 1, []))
    monkeypatch.setattr(osm, "search", lambda *a, **k: (
        [l for l in map(osm.parse_element, odata["elements"]) if l], []))


def test_pipeline_end_to_end(monkeypatch):
    _fake_sources(monkeypatch)
    res = pipeline.run(SearchParams(source="both", api_key="KEY", min_score=20))
    names = [l.name for l in res.leads]
    assert "Far Away Foods" not in names            # outside 30 miles
    assert "Old Closed Warehouse" not in names      # permanently closed
    assert names.count("Smith's Marketplace") == 1  # google + osm merged
    assert names[0] == "Smith's Marketplace"        # highest score first
    comps = {l.name for l in res.leads if l.lead_type == "Competitor"}
    assert comps == {"Pro Baler", "Action Compaction Services"}  # kept despite low score
    assert res.stats["competitors flagged"] == 2


def test_pipeline_osm_only_without_key(monkeypatch):
    _fake_sources(monkeypatch)
    res = pipeline.run(SearchParams())
    assert all("osm" in l.sources for l in res.leads)
    assert any("No Google Places API key" in w for w in res.warnings)


def test_pipeline_google_without_key_errors():
    with pytest.raises(PipelineError):
        pipeline.run(SearchParams(source="google"))


def test_exports(monkeypatch):
    _fake_sources(monkeypatch)
    params = SearchParams(source="both", api_key="KEY")
    res = pipeline.run(params)
    csv_text = to_csv_bytes(res.leads).decode("utf-8-sig")
    assert csv_text.splitlines()[0].startswith("Score,Tier,Lead Type,Flags,Business Name")
    assert "(801) 328-1683" in csv_text
    wb = load_workbook(io.BytesIO(to_xlsx_bytes(res.leads, res.run_info(params))))
    assert wb.sheetnames == ["Leads", "Run Info"]
    assert wb["Leads"].max_row == len(res.leads) + 1
    info = {r[0]: r[1] for r in wb["Run Info"].iter_rows(values_only=True) if r and r[0]}
    assert "api_key" not in info


def test_format_phone():
    assert format_phone("+1 801-555-0100") == "(801) 555-0100"
    assert format_phone("ext 12") == "ext 12"
