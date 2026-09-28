import pytest

from leadgen import config, pipeline
from leadgen.dedupe import dedupe
from leadgen.export import COLUMNS, to_xlsx_bytes
from leadgen.http import HttpError
from leadgen.models import Lead
from leadgen.pipeline import SearchParams
from leadgen.scoring import score_lead
from leadgen.sources import SourceError, osm, yelp

YELP_KEY = "y" * 128


@pytest.fixture(autouse=True)
def no_cache(tmp_path, monkeypatch):
    monkeypatch.setattr("leadgen.http.CACHE_DIR", tmp_path / "cache")


def _biz(i, **extra):
    biz = {"id": f"id{i}", "name": f"Biz {i}", "review_count": 10,
           "coordinates": {"latitude": 40.76, "longitude": -111.89},
           "location": {"address1": "1 Main St", "city": "Salt Lake City", "state": "UT",
                        "zip_code": "84101"},
           "categories": [{"alias": "grocery", "title": "Grocery"}], "is_closed": False}
    biz.update(extra)
    return biz


def _fake_yelp(monkeypatch, total=60, remaining=None, fail=None):
    """Serve `total` results per search; remaining(n) gives the quota header after call n."""
    calls = []

    def fake(method, url, *, params=None, headers=None, response_headers=None, **kw):
        calls.append(dict(params))
        assert headers["Authorization"] == f"Bearer {YELP_KEY}"
        if remaining is not None and response_headers is not None:
            response_headers["RateLimit-Remaining"] = str(remaining(len(calls)))
        if fail:
            fail(params, len(calls))
        start, n = params["offset"], params["limit"]
        return {"total": total,
                "businesses": [_biz(f"{params.get('term') or params['categories']}-{k}")
                               for k in range(start, min(start + n, total))]}

    monkeypatch.setattr(yelp, "request_json", fake)
    return calls


def test_parse_business():
    lead = yelp.parse_business({
        "id": "abc", "name": " Smith's Marketplace ", "review_count": 321, "is_closed": False,
        "url": "https://www.yelp.com/biz/smiths?adjust_creative=x&utm_source=y",
        "display_phone": "(801) 555-0100", "phone": "+18015550100",
        "coordinates": {"latitude": 40.75, "longitude": -111.87},
        "location": {"address1": "455 S 500 E", "address2": "Ste 2", "city": "Salt Lake City",
                     "state": "UT", "zip_code": "84102"},
        "categories": [{"alias": "grocery", "title": "Grocery"},
                       {"alias": "pharmacy", "title": "Pharmacy"}],
    }, "grocery stores")
    assert lead.name == "Smith's Marketplace" and lead.source == "yelp" and lead.source_id == "abc"
    assert lead.address == "455 S 500 E Ste 2" and lead.zip == "84102"
    assert lead.phone == "(801) 555-0100" and lead.yelp_reviews == 321
    assert lead.raw_categories == ["yelp:grocery", "yelp:pharmacy"]
    assert lead.primary_category == "Grocery, Pharmacy"
    assert lead.map_url == "https://www.yelp.com/biz/smiths"
    assert lead.business_status == "OPERATIONAL" and lead.search_terms == ["grocery stores"]
    assert yelp.parse_business(_biz(1, is_closed=True)).business_status == "CLOSED_PERMANENTLY"


def test_category_and_word_searches_page_by_offset(monkeypatch):
    calls = _fake_yelp(monkeypatch, total=120)
    leads, n, warnings = yelp.search(40.76, -111.89, 20, ["baler", "grocery stores"], YELP_KEY,
                                     max_requests=100)
    assert n == 6 and len(leads) == 240 and not warnings
    assert [(c.get("term"), c.get("categories"), c["offset"]) for c in calls[:2]] == [
        ("baler", None, 0), (None, "grocery,intlgrocery,organic_stores", 0)]
    assert calls[1]["sort_by"] == "review_count" and "sort_by" not in calls[0]
    assert [c["offset"] for c in calls] == [0, 0, 50, 50, 100, 100]
    assert all(c["radius"] <= 40000 for c in calls)


def test_offset_plus_limit_never_passes_240(monkeypatch):
    calls = _fake_yelp(monkeypatch, total=1000)
    leads, n, _ = yelp.search(40.76, -111.89, 20, ["a"], YELP_KEY, max_requests=50)
    assert [(c["offset"], c["limit"]) for c in calls] == [(0, 50), (50, 50), (100, 50),
                                                          (150, 50), (200, 40)]
    assert len(leads) == 240


def test_default_cap_and_wide_areas_use_grid(monkeypatch):
    calls = _fake_yelp(monkeypatch, total=10)
    queries = yelp.queries_for(["baler"])
    assert queries[:3] == ["baler", "Pro Baler", "Action Compaction"]
    assert queries[3:] == list(config.YELP_SEARCHES)
    yelp.search(40.76, -111.89, 30, queries, YELP_KEY)
    assert len(calls) == len(queries) * 7           # 30 miles needs the 7-cell grid
    assert yelp.grid_for(20, 1) == 1 and yelp.grid_for(30, 1) == 7 and yelp.grid_for(60, 7) == 19


def test_stops_before_daily_quota_runs_out(monkeypatch):
    calls = _fake_yelp(monkeypatch, total=10, remaining=lambda n: 8 - n)
    leads, n, warnings = yelp.search(40.76, -111.89, 20, list("abcdef"), YELP_KEY)
    assert n == 3 and len(calls) == 3 and len(leads) == 30
    assert any("daily limit is almost used up (5 calls left" in w for w in warnings)


def test_quota_error_keeps_results_and_stops(monkeypatch):
    def fail(params, n):
        if n == 2:
            raise HttpError('returned HTTP 429: {"error": {"code": "ACCESS_LIMIT_REACHED"}}')
    calls = _fake_yelp(monkeypatch, total=10, fail=fail)
    leads, n, warnings = yelp.search(40.76, -111.89, 20, list("abcd"), YELP_KEY)
    assert len(calls) == 2 and len(leads) == 10
    assert any("daily limit was reached" in w for w in warnings)
    assert not any("ACCESS_LIMIT_REACHED" in w for w in warnings)


def test_bad_key_raises(monkeypatch):
    def fail(params, n):
        raise HttpError('returned HTTP 401: {"error": {"code": "TOKEN_INVALID"}}')
    _fake_yelp(monkeypatch, fail=fail)
    with pytest.raises(SourceError):
        yelp.search(40.76, -111.89, 20, ["a"], YELP_KEY)


def test_complete_searches_are_cached(monkeypatch):
    calls = _fake_yelp(monkeypatch, total=10)
    yelp.search(40.76, -111.89, 20, ["a"], YELP_KEY)
    leads, n, _ = yelp.search(40.76, -111.89, 20, ["a"], YELP_KEY)
    assert len(calls) == 1 and n == 0 and len(leads) == 10


def test_yelp_key_in_google_setting_is_used_for_yelp(monkeypatch):
    google, yelp_key, notes = SearchParams(api_key=YELP_KEY).resolved_keys()
    assert (google, yelp_key) == ("", YELP_KEY) and notes
    real_google = "AIza" + "x" * 35
    assert SearchParams(api_key=real_google).resolved_keys() == (real_google, "", [])
    monkeypatch.setenv("YELP_API_KEY", YELP_KEY)
    assert SearchParams().resolved_keys() == ("", YELP_KEY, [])


def _yelp_lead(name, cats, reviews=0, **kw):
    return Lead(name=name, lat=40.76, lon=-111.89, source="yelp", source_id=name,
                raw_categories=[f"yelp:{c}" for c in cats], yelp_reviews=reviews, **kw)


def test_scoring_uses_yelp_categories_and_reviews():
    smiths = score_lead(_yelp_lead("Smith's Marketplace", ["grocery", "pharmacy"], 450))
    assert smiths.category_key == "grocery" and smiths.score == 35 + 20 + 15
    assert any("Yelp category" in r for r in smiths.reasons)
    assert any("450 Yelp reviews" in r for r in smiths.reasons)
    vet = score_lead(_yelp_lead("Cottonwood Animal Hospital", ["vet"], 500))
    assert vet.category_key == "" and vet.score == 15
    hauler = score_lead(_yelp_lead("Junk Pros", ["junkremovalandhauling"]))
    assert hauler.lead_type.startswith("Industry")
    plant = score_lead(_yelp_lead("Kiitos Brewing", ["breweries"]))
    assert plant.category_key == "food_production"
    taproom = score_lead(_yelp_lead("Kiitos Brewing", ["breweries", "pubs"]))
    assert taproom.category_key == "food_service"
    assert score_lead(_yelp_lead("Sunrise Apartments", ["apartments"])).category_key == "multifamily"
    both = Lead(name="Costco", lat=0, lon=0, source="google", source_id="g",
                raw_categories=["warehouse_store"], rating_count=150, yelp_reviews=500)
    assert [r for r in score_lead(both).reasons if "busy" in r] == ["+15 busy site (500 Yelp reviews)"]
    kw = score_lead(_yelp_lead("Green Earth", ["recyclingcenter"]), ["recycling"])
    assert kw.matched_keywords == ["recycling"]


def test_dedupe_merges_yelp_with_google_and_osm():
    g = Lead(name="Smith's Food & Drug", lat=40.7600, lon=-111.8900, source="google",
             source_id="g1", phone="+1 801-555-0100", website="https://smiths.com",
             business_status="OPERATIONAL", rating_count=900)
    y = _yelp_lead("Smith's Food and Drug", ["grocery"], 120, phone="(801) 555-0100")
    y.lat, y.lon = 40.7605, -111.8904
    y.business_status = "CLOSED_PERMANENTLY"
    [m] = dedupe([y, g])
    assert m.source == "google" and m.sources == ["google", "yelp"]
    assert m.yelp_reviews == 120 and m.business_status == "OPERATIONAL"   # Google's word wins

    closed = _yelp_lead("Old Market", ["grocery"], business_status="CLOSED_PERMANENTLY")
    osm_copy = Lead(name="Old Market", lat=40.76, lon=-111.89, source="osm", source_id="n1",
                    raw_categories=["shop=supermarket"])
    [m] = dedupe([osm_copy, closed])
    assert m.business_status == "CLOSED_PERMANENTLY" and m.sources == ["osm", "yelp"]


def test_pipeline_auto_uses_yelp_key_and_hides_it(monkeypatch):
    seen = {}

    def fake_search(lat, lon, radius, queries, key, grid, max_requests, progress):
        seen.update(queries=queries, key=key)
        lead = yelp.parse_business(_biz(1, name="Smith's Marketplace", review_count=450),
                                   queries[0])
        return [lead], 7, []
    monkeypatch.setattr(yelp, "search", fake_search)
    monkeypatch.setattr(osm, "search", lambda *a, **k: ([], []))
    monkeypatch.setenv("YELP_API_KEY", YELP_KEY)
    params = SearchParams(keywords=["baler"])
    res = pipeline.run(params)
    assert seen["key"] == YELP_KEY and seen["queries"][0] == "baler"
    assert res.stats["yelp requests"] == 7 and res.leads[0].sources == ["yelp"]
    info = res.run_info(params)
    assert YELP_KEY not in str(info) and "yelp_api_key" not in info
    assert "Yelp Reviews" in [c for c, _, _ in COLUMNS]
    assert YELP_KEY.encode() not in to_xlsx_bytes(res.leads, info)


def test_pipeline_yelp_source_needs_key():
    with pytest.raises(pipeline.PipelineError):
        pipeline.run(SearchParams(source="yelp"))
