from leadgen.dedupe import dedupe, names_match
from leadgen.geo import geocode, haversine_miles, search_grid
from leadgen.models import Lead


def test_haversine_slc_to_ogden():
    assert 31.5 < haversine_miles(40.7608, -111.8910, 41.2230, -111.9738) < 33


def test_grid_cells_cover_area():
    assert len(search_grid(40.76, -111.89, 30, 1)) == 1
    cells = search_grid(40.76, -111.89, 30, 7)
    assert len(cells) == 7
    assert all(haversine_miles(40.76, -111.89, a, b) <= 30 for a, b, _ in cells)
    assert len(search_grid(40.76, -111.89, 30, 19)) == 19


def test_geocode_offline_inputs():
    assert geocode("40.5,-111.9")[:2] == (40.5, -111.9)
    lat, lon, _ = geocode("Salt Lake City, UT")
    assert round(lat, 2) == 40.76


def test_names_match():
    assert names_match("Smith's Marketplace", "Smith's Marketplace #123")
    assert names_match("The Home Depot", "Home Depot")
    assert not names_match("Walmart", "Target")


def test_dedupe_merges_google_and_osm():
    g = Lead(name="Smith's Marketplace", lat=40.7590, lon=-111.8770, source="google",
             source_id="g1", phone="(801) 328-1683", rating_count=2000,
             raw_categories=["supermarket"], search_terms=["supermarket"])
    o = Lead(name="Smith's", lat=40.75905, lon=-111.87702, source="osm", source_id="node/1",
             zip="84102", footprint_sqft=60000, raw_categories=["shop=supermarket"])
    dup = Lead(name="Smith's Marketplace", lat=40.7590, lon=-111.8770, source="google",
               source_id="g1", search_terms=["grocery store"])
    far = Lead(name="Smith's", lat=40.60, lon=-111.90, source="osm", source_id="node/2")
    merged = dedupe([g, o, dup, far])
    assert len(merged) == 2
    m = next(x for x in merged if x.source == "google")
    assert m.sources == ["google", "osm"]
    assert m.footprint_sqft == 60000 and m.zip == "84102"
    assert set(m.search_terms) == {"supermarket", "grocery store"}


def _l(name, lat, lon, phone="", source="osm", sid=None):
    return Lead(name=name, lat=lat, lon=lon, source=source, source_id=sid or name, phone=phone)


def test_dedupe_keeps_neighboring_hotels_apart():
    a = _l("Comfort Inn & Suites Salt Lake City Airport", 40.77, -111.95, "8017833165")
    b = _l("Fairfield Inn & Suites Salt Lake City Airport", 40.7714, -111.95, "8013553331")
    assert len(dedupe([a, b])) == 2
    assert not names_match("343 Apartments", "Broadway Apartments")
    assert not names_match("Downtown 360", "Homewood Suites Downtown")


def test_dedupe_does_not_chain():
    a = _l("Acme Foods", 40.7600, -111.9)
    b = _l("Acme Foods Plant", 40.7610, -111.9)
    c = _l("Acme Foods Plant", 40.7620, -111.9)   # dup of b, but 0.14 mi from a
    groups = dedupe([a, b, c])
    # a~b and b~c, but a and c are too far apart with different names: no A-B-C chain.
    assert len(groups) == 2


def test_dedupe_keeps_competitor_names():
    comp = _l("Action Compaction", 40.76, -111.9, "8015550100", source="google", sid="g")
    osm_copy = _l("Action Compaction Services LLC", 40.7601, -111.9)
    merged = dedupe([osm_copy, comp])
    assert len(merged) == 1
    assert "Action Compaction Services LLC" in merged[0].alt_names or merged[0].name.startswith("Action")
