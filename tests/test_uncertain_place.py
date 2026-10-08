"""Text that is not a place, or a match that is only a road, a trail or a business, never
gets the day's one search without the person being told: "1900-01-01" is not looked up
at all, and "Springfield" (only Springfield Road in Salt Lake City) is named in the
confirmation and needs its own yes before the search starts. Towns, ZIP codes and street
addresses go straight to the normal confirmation. The place lookups are stand-ins here,
never the real Nominatim or Google."""

import time

import pytest

from leadgen import daily, geo, web
from leadgen.http import HttpError
from leadgen.pipeline import RunResult

SPRINGFIELD_ROAD = {"lat": "40.7801", "lon": "-111.9795", "class": "highway", "type": "residential",
                    "name": "Springfield Road",
                    "display_name": "Springfield Road, Westpointe, Salt Lake City, Salt Lake County, Utah, 84116, "
                                    "United States",
                    "address": {"road": "Springfield Road", "suburb": "Westpointe", "city": "Salt Lake City",
                                "county": "Salt Lake County", "state": "Utah", "postcode": "84116"}}
TRAIL = {"lat": "40.6159", "lon": "-111.5862", "class": "highway", "type": "path", "name": "Wasatch Crest Trail",
         "display_name": "Wasatch Crest Trail, Brighton, Salt Lake County, Utah, 84055, United States",
         "address": {"path": "Wasatch Crest Trail", "town": "Brighton", "county": "Salt Lake County",
                     "state": "Utah"}}
ROAD_WORDS = "We found a road called Springfield Road in Salt Lake City. Is that what you meant?"


def _nominatim(*answers):
    """A stand-in for every place lookup: Nominatim answers `answers`, in order of asking
    (the Utah-only lookup first), then nothing; the ZIP lookup knows no ZIP code."""
    asked = []
    left = list(answers)

    def lookup(method, url, params=None, **kw):
        asked.append((url, dict(params or {})))
        if "zippopotam" in url:
            raise HttpError(f"{url} returned HTTP 404: {{}}", 404)
        assert "nominatim.openstreetmap.org" in url, url
        return left.pop(0) if left else []
    return lookup, asked


def _wait(client, job):
    for _ in range(400):
        body = client.get(f"/status/{job}").get_json()
        if body["state"] != "running":
            return body
        time.sleep(0.05)
    raise AssertionError("the search never finished")


# ---- text that is not a place is never looked up

@pytest.mark.parametrize("typed", ["1900-01-01", "01/02/2023", "12345678", "42", "3.14", "801-555-0100", "---"])
def test_text_without_a_letter_that_isnt_a_zip_code_is_not_a_place(monkeypatch, typed):
    monkeypatch.setattr(geo, "request_json", lambda *a, **k: pytest.fail("looked up online"))
    with pytest.raises(geo.GeocodeError, match="Could not find the place"):
        geo.geocode(typed, "")


def test_five_digits_the_zip_lookup_doesnt_know_are_not_looked_up_elsewhere(monkeypatch):
    lookup, asked = _nominatim([SPRINGFIELD_ROAD])
    monkeypatch.setattr(geo, "request_json", lookup)
    with pytest.raises(geo.GeocodeError, match="Could not find the place '00000'"):
        geo.geocode("00000", "KEY")
    assert [url for url, _ in asked] == ["https://api.zippopotam.us/us/00000"]


def test_a_zip_code_lookup_that_is_down_still_falls_back_to_the_others(monkeypatch):
    def lookups(method, url, params=None, **kw):
        if "zippopotam" in url:
            raise HttpError(f"{url}: ConnectionError(timed out)")
        return [{"lat": "40.79", "lon": "-111.96", "class": "place", "type": "postcode",
                 "display_name": "84116, Salt Lake City, Utah, United States"}]
    monkeypatch.setattr(geo, "request_json", lookups)
    found = geo.geocode("84116", "")
    assert found[2].startswith("84116") and found.doubt == ""


# ---- what the match is, from the lookup's own class and type

def test_a_road_or_a_trail_is_named_as_such(monkeypatch):
    lookup, asked = _nominatim([SPRINGFIELD_ROAD])
    monkeypatch.setattr(geo, "request_json", lookup)
    found = geo.geocode("Springfield", "")
    assert found[:2] == (40.7801, -111.9795) and found.doubt == ROAD_WORDS
    assert asked[0][1]["addressdetails"] == 1 and asked[0][1]["bounded"] == 1
    monkeypatch.setattr(geo, "request_json", _nominatim([TRAIL])[0])
    assert geo.geocode("Jan 1 1900", "").doubt == ("We found a trail called Wasatch Crest Trail in Brighton. "
                                                   "Is that what you meant?")


@pytest.mark.parametrize("match, words", [
    ({"class": "leisure", "type": "park", "name": "Liberty Park", "address": {"city": "Salt Lake City"}},
     "We found a park called Liberty Park in Salt Lake City."),
    ({"class": "shop", "type": "supermarket", "name": "Smith's", "address": {"city": "Murray"}},
     "We found a business called Smith's in Murray."),
    ({"class": "natural", "type": "peak", "name": "Mount Olympus", "address": {"county": "Salt Lake County"}},
     "We found a mountain peak called Mount Olympus in Salt Lake County."),
    ({"class": "highway", "type": "primary", "name": "Springfield Road",
      "address": {"city": "Springfield", "state": "Illinois"}},
     "We found a road called Springfield Road in Springfield, Illinois."),
    ({"class": "waterway", "type": "river", "display_name": "Jordan River, Utah"},
     "We found a river or canal called Jordan River."),
])
def test_other_features_are_named_too(monkeypatch, match, words):
    monkeypatch.setattr(geo, "request_json", _nominatim([{"lat": "40.7", "lon": "-111.9", **match}])[0])
    assert geo.geocode("Somewhere odd", "").doubt == f"{words} Is that what you meant?"


@pytest.mark.parametrize("typed, match", [
    ("Springfield", {"class": "place", "type": "town", "display_name": "Springfield, Utah"}),
    ("Holladay", {"class": "boundary", "type": "administrative", "display_name": "Holladay, Utah"}),
    ("84116", {"class": "place", "type": "postcode", "display_name": "84116, Utah"}),
    ("4800 S State St, Murray", {"class": "place", "type": "house", "display_name": "4800, State Street"}),
    ("4800 S State St, Murray", {"class": "shop", "type": "supermarket", "display_name": "Smith's, 4800",
                                 "address": {"house_number": "4800", "road": "South State Street"}}),
    ("Somewhere", {"display_name": "Somewhere, Utah"}),        # the lookup didn't say what it is
])
def test_a_town_a_zip_code_or_a_street_address_is_not_doubted(monkeypatch, typed, match):
    lookups = _nominatim([{"lat": "40.66", "lon": "-111.88", **match}])[0]

    def no_zip(method, url, params=None, **kw):
        if "zippopotam" in url:
            raise HttpError(f"{url}: ConnectionError(timed out)")
        return lookups(method, url, params)
    monkeypatch.setattr(geo, "request_json", no_zip)
    assert geo.geocode(typed, "").doubt == ""


def test_a_street_address_found_only_as_its_road_says_so(monkeypatch):
    road = {**SPRINGFIELD_ROAD, "name": "South State Street", "address": {"city": "Murray", "state": "Utah"}}
    monkeypatch.setattr(geo, "request_json", _nominatim([road])[0])
    assert geo.geocode("4800 S State St", "").doubt == ("We found a road called South State Street in Murray, but "
                                                        "not the street number typed. Is that what you meant?")


def test_a_town_or_address_is_preferred_over_a_road_of_the_same_name(monkeypatch):
    town = {"lat": "40.64", "lon": "-111.94", "class": "place", "type": "village", "display_name": "Springfield, UT"}
    monkeypatch.setattr(geo, "request_json", _nominatim([SPRINGFIELD_ROAD, town])[0])
    found = geo.geocode("Springfield", "")
    assert found[2] == "Springfield, UT" and found.doubt == ""


@pytest.mark.parametrize("types, words", [
    (["route"], ROAD_WORDS),
    (["establishment", "point_of_interest"],
     "We found a business or building called Springfield Road in Salt Lake City. Is that what you meant?"),
    (["locality", "political"], ""), (["street_address"], ""), (["postal_code"], ""), ([], ""),
])
def test_googles_result_types_say_the_same(monkeypatch, types, words):
    def google(method, url, params=None, **kw):
        assert "maps.googleapis.com" in url
        return {"status": "OK", "results": [{
            "geometry": {"location": {"lat": 40.78, "lng": -111.98}}, "types": types,
            "formatted_address": "Springfield Rd, Salt Lake City, UT 84116, USA",
            "address_components": [{"long_name": "Springfield Road", "types": ["route"]},
                                   {"long_name": "Salt Lake City", "types": ["locality", "political"]},
                                   {"long_name": "Utah", "types": ["administrative_area_level_1", "political"]}]}]}
    monkeypatch.setattr(geo, "request_json", google)
    assert geo.geocode("Springfield", "KEY").doubt == words


# ---- the page: told before the search can start, and asked for its own yes

def test_the_page_is_told_and_the_search_needs_a_yes_for_that_place(monkeypatch):
    monkeypatch.setattr(geo, "request_json", lambda *a, **k: [SPRINGFIELD_ROAD])
    ran = []
    monkeypatch.setattr(web.finding, "run", lambda params, progress: ran.append(params) or RunResult(
        [], params.center, params.place, [], {"leads kept": 0}))
    client = web.create_app().test_client()
    place = client.get("/place?location=Springfield").get_json()
    assert place["uncertain"] == ROAD_WORDS and not place["outside"] and place["miles"] < 10
    res = client.post("/search", data={"location": "Springfield"})
    body = res.get_json()
    assert res.status_code == 409 and body["confirm_uncertain"] and body["field"] == "location"
    assert body["error"] == f"{ROAD_WORDS} Check the place, or confirm that you want to search there."
    assert not ran and not daily.history()["searches"]
    # A yes for another place doesn't count; the yes for this one starts it.
    assert client.post("/search", data={"location": "Springfield", "confirm_place": "40.5,-111.9"}).status_code == 409
    job = client.post("/search", data={"location": "Springfield", "confirm_place": place["confirm"]}).get_json()
    assert _wait(client, job["job_id"])["state"] == "done"
    assert ran[0].place == SPRINGFIELD_ROAD["display_name"]


def test_not_a_place_is_refused_at_the_field_and_spends_nothing(monkeypatch):
    monkeypatch.setattr(geo, "request_json", lambda *a, **k: pytest.fail("looked up online"))
    client = web.create_app().test_client()
    res = client.get("/place?location=1900-01-01")
    assert res.status_code == 400 and res.get_json()["field"] == "location"
    assert res.get_json()["error"].startswith("Could not find the place '1900-01-01'.")
    assert client.post("/search", data={"location": "1900-01-01"}).status_code == 400
    assert not daily.history()["searches"]


@pytest.mark.parametrize("typed", ["84116", "Provo", "876 Fortune Rd, Salt Lake City", "876 Fortune Road"])
def test_towns_zip_codes_and_aarcos_address_go_straight_to_the_confirmation(monkeypatch, typed):
    def zip_only(method, url, params=None, **kw):
        assert "zippopotam" in url, url                          # the others are known offline
        return {"places": [{"place name": "Salt Lake City", "state abbreviation": "UT",
                            "latitude": "40.7563", "longitude": "-111.8994"}]}
    monkeypatch.setattr(geo, "request_json", zip_only)
    body = web.create_app().test_client().get("/place", query_string={"location": typed}).get_json()
    assert body["uncertain"] == "", body           # (Provo is outside the area: asked about that as before)
