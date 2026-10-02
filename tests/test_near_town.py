"""A lead without a street address or city still shows a town (and ZIP): the one its map
position is near, worked out offline and labelled "near ...", in the pages and the
downloads, so same-named stores can be told apart. Nothing saved is changed."""

import csv
import io
import json

from openpyxl import load_workbook

from leadgen import config, places, saved, store, web
from leadgen.models import Lead
from leadgen.scoring import score_lead


def _lowes(sid, lat, lon, **kw):
    lead = Lead(name="Lowe's", lat=lat, lon=lon, source="osm", source_id=sid,
                raw_categories=["shop=doityourself"], **kw)
    return score_lead(lead, config.DEFAULT_KEYWORDS)


def test_the_nearest_town_and_zip_come_from_the_coordinates():
    assert places.near(*config.OWN_COORDS) == places.Near("Salt Lake City", "UT", "84104")
    assert places.near(40.6097, -111.9391).text() == "near West Jordan, UT 84088"
    assert places.near(40.5649, -111.8389).town == "Sandy"
    assert places.near(41.06, -111.97).town == "Layton"
    assert places.near(None, None) is None and places.near(0.0, 0.0) is None
    assert places.near(25.76, -80.19) is None            # Miami: not near any town in the table


def _three_lowes():
    leads = [_lowes("w1", 40.6097, -111.9391), _lowes("s1", 40.5649, -111.8389),
             _lowes("m1", 40.6669, -111.8880, address="5000 S State St", city="Murray", zip="84107")]
    saved.save_search(leads, config.DEFAULT_KEYWORDS)
    return leads


def test_the_leads_page_shows_which_town_each_store_is_near():
    _three_lowes()
    client = web.create_app().test_client()
    rows = client.get("/leads?tab=all&q=lowe").get_json()["leads"]
    near = sorted(r["near"] for r in rows)
    assert near == ["", "near Sandy, UT 84094", "near West Jordan, UT 84088"]
    assert all(r["city"] in ("", "Murray") for r in rows)        # never passed off as the city
    # The filter and the city sort know the town too.
    assert [r["near"] for r in client.get("/leads?tab=all&q=west%20jordan").get_json()["leads"]] == \
        ["near West Jordan, UT 84088"]
    by_city = client.get("/leads?tab=all&sort=city&dir=asc").get_json()["leads"]
    assert [r["city"] or r["near"] for r in by_city] == ["Murray", "near Sandy, UT 84094",
                                                        "near West Jordan, UT 84088"]
    # The saved rows themselves are untouched.
    with store.connect() as db:
        stored = [json.loads(lead) for (lead,) in db.all("SELECT lead FROM leads")]
    assert all("near" not in lead for lead in stored)
    assert sorted(lead["city"] for lead in stored) == ["", "", "Murray"]


def test_every_downloaded_row_with_coordinates_has_a_town():
    _three_lowes()
    client = web.create_app().test_client()
    rows = list(csv.DictReader(io.StringIO(client.get("/download/saved.csv").data.decode("utf-8-sig"))))
    assert [r for r in rows if r["Latitude"] and not r["City"]] == []
    cities = sorted((r["City"], r["State"], r["ZIP"]) for r in rows)
    assert cities == [("Murray", "", "84107"), ("near Sandy", "UT", "near 84094"),
                      ("near West Jordan", "UT", "near 84088")]
    book = load_workbook(io.BytesIO(client.get("/download/saved.xlsx").data))
    info = {row[0]: row[1] for row in book["Run Info"].iter_rows(values_only=True) if row[0]}
    assert "worked out from the map position" in info["City / ZIP “near …”"]


# ---- a listed city is shown as the town's own name

def test_listed_towns_are_tidied_to_the_census_name():
    # Values from a real Salt Lake area search of the map data.
    for given, shown in [("CLEARFIELD", "Clearfield"), ("american Fork", "American Fork"),
                         ("West Jordan City", "West Jordan"), ("Woods Cross City", "Woods Cross"),
                         ("South Salt Lake City", "South Salt Lake"), ("West Valley", "West Valley City"),
                         ("Saratoga Spring", "Saratoga Springs"), ("Draper City (Sl Co)", "Draper"),
                         ("Layton, UT", "Layton"), ("Ogden Utah", "Ogden"), ("W Jordan", "West Jordan"),
                         ("N. Salt Lake", "North Salt Lake"), ("SLC", "Salt Lake City"),
                         ("Marriott Slaterville", "Marriott-Slaterville"), ("Salt Lake City", "Salt Lake City")]:
        assert places.tidy_town(given) == shown, given
    # A short form the town it is near starts with: "la" at a Layton hotel (ZIP 84041).
    assert places.tidy_town("la", "UT", 41.0909, -111.9763, "84041") == "Layton"
    assert places.tidy_town("la", zip_code="84041") == "Layton"
    assert places.tidy_town("AF", "UT", 40.38, -111.79) == "American Fork"
    # Unknown towns stay, made readable; one or two letters that name nothing, or only a
    # state, say no more than no city at all (the page shows the town it is near instead).
    assert places.tidy_town("HILL AIR FORCE BASE") == "Hill Air Force Base"
    assert places.tidy_town("Hill Airforce Base") == "Hill Airforce Base"
    assert places.tidy_town("San Francisco", "CA") == "San Francisco"
    assert places.tidy_town("xq", "UT", 40.38, -111.79) == "" and places.tidy_town("Utah") == ""
    assert places.state_code("utah") == "UT" and places.state_code("ca") == "ca"


def _town_lead(sid, city, lat, lon, zip_code="", state=""):
    lead = Lead(name=f"Distribution {sid}", lat=lat, lon=lon, source="osm", source_id=sid, city=city,
                zip=zip_code, state=state, address=f"{len(sid)} Main St", raw_categories=["building=warehouse"])
    return score_lead(lead, config.DEFAULT_KEYWORDS)


def test_the_pages_and_downloads_show_tidy_towns_and_the_saved_rows_keep_theirs():
    saved.save_search([_town_lead("c", "CLEARFIELD", 41.1108, -112.0261, "84015", "ut"),
                       _town_lead("l", "la", 41.0909, -111.9763, "84041"),
                       _town_lead("a", "american Fork", 40.3769, -111.7958, "84003"),
                       _town_lead("w", "West Jordan City", 40.6097, -111.9391, "84088")],
                      config.DEFAULT_KEYWORDS)
    client = web.create_app().test_client()
    by_city = client.get("/leads?tab=all&sort=city&dir=asc").get_json()["leads"]
    assert [r["city"] for r in by_city] == ["American Fork", "Clearfield", "Layton", "West Jordan"]
    assert [r["near"] for r in by_city] == ["", "", "", ""]
    # The filter finds a business by its town however the listing spelled it.
    assert [r["city"] for r in client.get("/leads?tab=all&q=clearfield").get_json()["leads"]] == ["Clearfield"]
    assert [r["city"] for r in client.get("/leads?tab=all&q=layton").get_json()["leads"]] == ["Layton"]
    rows = list(csv.DictReader(io.StringIO(client.get("/download/saved.csv").data.decode("utf-8-sig"))))
    assert sorted((r["City"], r["State"]) for r in rows) == [
        ("American Fork", ""), ("Clearfield", "UT"), ("Layton", ""), ("West Jordan", "")]
    for r in rows:
        town = r["City"]
        assert not town.isupper() and not town.islower() and len(town) > 2
    # Only what is shown changes: the saved rows keep the text the map data gave.
    with store.connect() as db:
        stored = sorted(json.loads(lead)["city"] for (lead,) in db.all("SELECT lead FROM leads"))
    assert stored == ["CLEARFIELD", "West Jordan City", "american Fork", "la"]
