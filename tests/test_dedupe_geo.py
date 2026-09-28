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
