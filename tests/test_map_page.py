"""The Map page's data (/map-data): AARCO's pin, its area and the areas a search asks the
map data in, how the latest search of the area covered each, and a pin for every saved
lead; and what a day's search record keeps for it (its centre, and the parts no server
answered for). Runs on SQLite, and on Postgres with LEADGEN_TEST_DATABASE_URL."""

import json
import time

import pytest
from test_fill_in import _servers, filling  # noqa: F401  (a fixture)

from leadgen import area_map, calls, config, contacts, daily, fillin, marks, pipeline, saved, store, web
from leadgen.geo import haversine_miles
from leadgen.models import Lead
from leadgen.scoring import score_lead
from leadgen.sources import osm
from leadgen.web.common import LoadError

AARCO = config.SERVICE_CENTER


def _lead(name, sid, lat=40.75, lon=-111.9, cats=("shop=supermarket",), **kw):
    lead = Lead(name=name, source="osm", source_id=sid, lat=lat, lon=lon, raw_categories=list(cats),
                city="Salt Lake City", **kw)
    score_lead(lead, config.DEFAULT_KEYWORDS)
    return lead


def _data(client=None):
    res = (client or web.create_app().test_client()).get("/map-data")
    assert res.status_code == 200 and res.mimetype == "application/json"
    return res.get_json()


def _pins(data):
    """The pins by business name, each as {field: value}."""
    return {row[data["fields"].index("name")]: dict(zip(data["fields"], row, strict=True)) for row in data["pins"]}


def _areas():
    return {a.name: a for a in area_map.areas()}


def _record(info, day="2026-09-30", at=None):
    """A day's search as the search history keeps it."""
    with store.connect() as db:
        db.run("INSERT INTO searches (day, at, info) VALUES (?, ?, ?)",
               (day, time.time() - 3600 if at is None else at, json.dumps(info)))


def _aarcos(**info):
    """A finished search of AARCO's area (as recorded now: with its centre)."""
    return {"location": config.OWN_ADDRESS, "radius": config.SERVICE_AREA_MILES, "source": "auto",
            "place": f"{config.OWN_COMPANY}, {config.OWN_ADDRESS}", "miles": 0.0, "center": list(AARCO),
            "leads": 40, "new": 4, "details": {"osm areas searched": "about 9 of 9 areas"}, **info}


def _statuses(data):
    return {a["name"]: a["status"] for a in data["areas"]}


# ---- what is drawn

def test_the_map_needs_the_login_like_every_page():
    client = web.create_app(password="s3cret", username="Matt").test_client()
    assert client.get("/map-data").status_code == 401
    client.post("/login", data={"username": "Matt", "password": "s3cret"})
    assert client.get("/map-data").status_code == 200


def test_aarco_its_area_and_the_nine_search_areas():
    data = _data()
    assert data["aarco"] == {"name": "AARCO", "company": config.OWN_COMPANY, "address": config.OWN_ADDRESS,
                             "lat": config.OWN_COORDS[0], "lon": config.OWN_COORDS[1]}
    assert data["center"] == list(AARCO) and data["miles"] == config.SERVICE_AREA_MILES == 30
    # The circle is 30 miles out all round.
    assert len(data["circle"]) == area_map.CIRCLE_POINTS
    assert all(abs(haversine_miles(*AARCO, lat, lon) - 30) < 0.3 for lat, lon in data["circle"])
    # The areas are the search's own ("N of 9 areas"), nearest first, each named and inside the circle.
    areas = data["areas"]
    assert [a["n"] for a in areas] == list(range(1, 10))
    assert len(osm.area_boxes(*AARCO, 30)) == 9
    assert areas[0]["name"] == "Salt Lake City"
    assert len({a["name"] for a in areas}) == 9 and all(a["name"][0].isupper() for a in areas)
    for area, drawn in zip(areas, area_map.areas(), strict=True):
        assert all(haversine_miles(*AARCO, lat, lon) < 30.2 for lat, lon in area["outline"])
        south, west, north, east = drawn.box
        assert south <= area["label"][0] <= north and west <= area["label"][1] <= east
        assert len(area["towns"]) <= 3 and area["name"] not in area["towns"]
    # AARCO's own pin stands in the middle area; its name is written clear of it.
    assert haversine_miles(*AARCO, *areas[0]["label"]) > 3
    # Before any search of the area, nothing is shaded and the page says why.
    assert {a["status"] for a in areas} == {"unknown"}
    assert data["coverage"] == "No search of AARCO's area has been recorded yet, so the areas aren't shaded."
    assert data["pins"] == [] and data["no_position"] == 0
    assert data["counts"] == {"unchecked": 0, "yes": 0, "no": 0, "competitor": 0}


def test_every_saved_business_with_a_position_is_a_pin_with_what_its_box_shows():
    yes = _lead("Smith's Marketplace", "s", phone="8015550101")
    no = _lead("Hampton Inn", "h", lat=40.6, cats=("tourism=hotel",))
    unchecked = _lead("Costco Wholesale", "c", lat=40.8, address="", zip="84104")
    rival = _lead("Pro Baler", "p", cats=("yelp:junkremoval",))
    own = _lead("AARCO Compactor", "a", lat=config.OWN_COORDS[0], lon=config.OWN_COORDS[1])
    parent = _lead("Ace Intermountain Recycling Center", "ace", lat=40.74, cats=("amenity=recycling",))
    nowhere = _lead("Nowhere Foods", "n", lat=0.0, lon=0.0)
    closed = _lead("Old Mill Foods", "m", lat=40.7, address="9 Mill Rd")
    saved.save_search([yes, no, unchecked, rival, own, parent, nowhere, closed], config.DEFAULT_KEYWORDS)
    saved.save_search([_lead("Old Mill Foods", "m", lat=40.7, address="9 Mill Rd",
                             business_status="CLOSED_PERMANENTLY")], config.DEFAULT_KEYWORDS)
    marks.set_mark(yes.uid, "yes", by="Dana")
    marks.set_mark(no.uid, "no", by="Dana")
    calls.log_call(yes.uid, "Follow Up", "Send a quote.")
    contacts.save(yes.uid, "(801) 555-0199", "Jane Doe", "Dana")

    data = _data()
    pins = _pins(data)
    assert set(pins) == {"Smith's Marketplace", "Hampton Inn", "Costco Wholesale", "Pro Baler", "AARCO Compactor",
                         "Ace Intermountain Recycling Center", "Old Mill Foods"}
    assert data["no_position"] == 1                       # Nowhere Foods: on the Leads page only
    assert data["counts"] == {"unchecked": 2, "yes": 1, "no": 1, "competitor": 3}
    assert data["fields"] == list(area_map.FIELDS)
    s = pins["Smith's Marketplace"]
    assert (s["key"], s["group"], s["lat"], s["lon"]) == (yes.uid, "yes", 40.75, -111.9)
    assert s["tier"] == yes.tier and s["score"] == yes.score
    assert s["phone"] == "(801) 555-0101" and s["verified_phone"] == "(801) 555-0199"
    assert s["outcome"] == "Follow Up" and s["called"] and not s["closed"] and s["kind"] == ""
    assert pins["Hampton Inn"]["group"] == "no" and pins["Hampton Inn"]["outcome"] == ""
    assert pins["Hampton Inn"]["called"] == ""
    assert pins["Costco Wholesale"]["group"] == "unchecked"
    assert pins["Costco Wholesale"]["address"].startswith("No street address")
    assert (pins["Pro Baler"]["group"], pins["Pro Baler"]["kind"]) == ("competitor", "Competitor")
    assert (pins["AARCO Compactor"]["group"], pins["AARCO Compactor"]["kind"]) == ("competitor", "Own company")
    ace = pins["Ace Intermountain Recycling Center"]           # AARCO's parent company's: its own too
    assert (ace["group"], ace["kind"]) == ("competitor", "Parent company")
    assert pins["Old Mill Foods"]["closed"] is True and pins["Old Mill Foods"]["address"].startswith("9 Mill Rd")
    # A mark made since is on the next answer (the saved list is read fresh).
    marks.set_mark(unchecked.uid, "yes", by="Dana")
    assert _pins(_data())["Costco Wholesale"]["group"] == "yes"


def test_the_map_opens_a_business_on_the_leads_page_by_its_id():
    one, other = _lead("Smith's Marketplace", "s"), _lead("Costco Wholesale", "c")
    saved.save_search([one, other], config.DEFAULT_KEYWORDS)
    client = web.create_app().test_client()
    rows = client.get(f"/leads?tab=all&lead={one.uid}").get_json()["leads"]
    assert [r["name"] for r in rows] == ["Smith's Marketplace"]
    assert client.get("/leads?tab=all&lead=nope").get_json()["leads"] == []
    assert len(client.get("/leads?tab=all").get_json()["leads"]) == 2


def test_the_map_says_so_when_the_saved_list_cannot_be_read(monkeypatch):
    def down():
        raise LoadError("The saved leads couldn't be read just now.")
    monkeypatch.setattr(web.mapping, "load_saved", down)
    res = web.create_app().test_client().get("/map-data")
    assert res.status_code == 503 and res.get_json()["error"] == "The saved leads couldn't be read just now."


def test_without_the_search_history_the_map_still_shows_unshaded(monkeypatch):
    saved.save_search([_lead("Smith's Marketplace", "s")], config.DEFAULT_KEYWORDS)

    def down(*args, **kw):
        raise store.Unavailable("database down")
    monkeypatch.setattr(area_map, "latest_search", down)
    data = _data()
    assert len(data["pins"]) == 1 and {a["status"] for a in data["areas"]} == {"unknown"}
    assert data["coverage"] == "The search history couldn't be read just now, so the areas aren't shaded."


def test_thousands_of_pins_stay_small_and_quick():
    leads = [_lead(f"Market {i}", f"m{i}", lat=40.5 + (i % 60) / 100, lon=-112.2 + (i // 60) / 100)
             for i in range(3000)]
    saved.save_search(leads, config.DEFAULT_KEYWORDS)
    client = web.create_app().test_client()
    _data(client)                                         # the saved list is read once
    started = time.monotonic()
    res = client.get("/map-data")
    took = time.monotonic() - started
    assert len(res.get_json()["pins"]) == 3000
    assert len(res.data) < 600_000 and took < 2.0, (len(res.data), took)


# ---- how the latest search of AARCO's area covered each area

def test_a_search_that_got_every_area_shades_them_all_searched():
    _record(_aarcos(map_areas={"missing": []}))
    data = _data()
    assert set(_statuses(data).values()) == {"searched"}
    assert data["coverage"].startswith("Shaded by the latest search of AARCO's area (")
    assert data["coverage"].endswith(": all 9 areas searched.")
    assert {a["status_text"] for a in data["areas"]} == {"Searched"}


def test_the_parts_no_server_answered_for_are_shaded_missed_or_partly():
    areas = _areas()
    north_west = next(a for a in area_map.areas() if a.n == 6)
    layton = areas["Layton"]
    south, west, north, east = layton.box
    quarter = [south, west, (south + north) / 2, (west + east) / 2]
    _record(_aarcos(partial=True, map_areas={"missing": [list(north_west.box), quarter]}))
    data = _data()
    statuses = _statuses(data)
    assert statuses.pop(north_west.name) == "missed" and statuses.pop("Layton") == "partly"
    assert set(statuses.values()) == {"searched"}
    assert data["coverage"].endswith(": 7 of the 9 areas searched in full.")
    texts = {a["name"]: a["status_text"] for a in data["areas"]}
    assert texts["Layton"] == "Partly searched: some of it didn't come in"
    assert texts[north_west.name] == "Not searched: the free map data didn't come in for it"


def test_areas_being_asked_again_in_the_background_say_so():
    layton = _areas()["Layton"]
    # The filling in's own list (it shrinks as areas answer) wins over the search's.
    _record(_aarcos(partial=True, map_areas={"missing": [list(a.box) for a in area_map.areas()]},
                    fill={"state": "filling", "left": 1, "boxes": [list(layton.box)], "where": "Layton",
                          "until": time.time() + 600}))
    data = _data()
    statuses = _statuses(data)
    assert statuses.pop("Layton") == "asking" and set(statuses.values()) == {"searched"}
    assert data["coverage"].endswith(": 8 of the 9 areas searched in full, the rest still being asked in the "
                                     "background.")
    # Once the filling in has every area, all are searched.
    _record(_aarcos(fill={"state": "complete", "left": 0, "boxes": []}), day="2026-10-01", at=time.time())
    assert set(_statuses(_data()).values()) == {"searched"}


def test_a_search_stopped_partway_or_whose_map_data_never_answered():
    _record(_aarcos(stopped="paused", map_areas={"known": False}))
    data = _data()
    assert set(_statuses(data).values()) == {"unknown"} and "stopped partway" in data["coverage"]
    _record(_aarcos(partial=True, map_areas={"missing": [None]}), day="2026-10-01", at=time.time())
    data = _data()
    assert set(_statuses(data).values()) == {"missed"}
    assert data["coverage"].endswith(": 0 of the 9 areas searched in full.")


def test_searches_recorded_before_the_areas_were_kept_are_read_from_their_words():
    names = [a.name for a in area_map.areas()]
    # Every area answered (nothing was incomplete).
    _record(_aarcos(), day="2026-09-01")
    assert set(_statuses(_data()).values()) == {"searched"}
    # The map data didn't answer at all.
    _record(_aarcos(partial=True, reason="Couldn't reach Google and the map data service, so 0 found."),
            day="2026-09-02")
    assert set(_statuses(_data()).values()) == {"missed"}
    # Its note named the areas still missing: those are missed and the others were searched.
    _record(_aarcos(partial=True, reason=f"Incomplete (about 7 of 9 areas searched; not yet: {names[4]} and "
                                         f"{names[6]})."), day="2026-09-03")
    statuses = _statuses(_data())
    assert [statuses[n] for n in names] == ["searched"] * 4 + ["missed", "searched", "missed", "searched", "searched"]
    # A list cut short ("and more"): the others aren't known.
    _record(_aarcos(partial=True, fill={"state": "gave_up", "left": 4, "where": f"{names[4]}, {names[5]} and more"}),
            day="2026-09-04")
    data = _data()
    statuses = _statuses(data)
    assert statuses[names[4]] == statuses[names[5]] == "missed"
    assert {statuses[n] for n in names if n not in names[4:6]} == {"unknown"}
    assert "whether the others were searched in full wasn't recorded then" in data["coverage"]
    # Incomplete with nothing said about which areas: none is shaded.
    _record(_aarcos(partial=True, reason="Incomplete."), day="2026-09-05")
    data = _data()
    assert set(_statuses(data).values()) == {"unknown"} and "which ones wasn't recorded then" in data["coverage"]


def test_a_record_whose_words_called_the_areas_parts_is_read_the_same():
    names = [a.name for a in area_map.areas()]
    _record(_aarcos(partial=True, reason=f"Incomplete (about 7 of 9 parts searched; not yet: {names[4]} and "
                                         f"{names[6]})."))
    data = _data()
    statuses = _statuses(data)
    assert [statuses[n] for n in names] == ["searched"] * 4 + ["missed", "searched", "missed", "searched", "searched"]
    assert data["coverage"].endswith(": 7 of the 9 areas searched in full.")


def test_only_a_search_of_aarcos_area_shades_it():
    _record(_aarcos(map_areas={"missing": []}), at=time.time() - 7200)
    newer = time.time() - 60
    # Searches since then of another place, another radius, the paid sources only, or not finished.
    _record(_aarcos(center=[40.23, -111.66], miles=48.1, map_areas={"missing": [None]}), day="2026-10-01", at=newer)
    _record(_aarcos(radius=10, map_areas={"missing": [None]}), day="2026-10-02", at=newer)
    _record(_aarcos(source="google", map_areas=None), day="2026-10-03", at=newer)
    unfinished = _aarcos(map_areas={"missing": [None]})
    del unfinished["leads"]
    _record(unfinished, day="2026-10-04", at=newer)
    assert set(_statuses(_data()).values()) == {"searched"}
    # A search recorded before the centre was kept counts by its distance from AARCO.
    old = _aarcos(partial=True, map_areas={"missing": [None]})
    del old["center"]
    _record(old, day="2026-10-05")
    assert set(_statuses(_data()).values()) == {"missed"}


# ---- a search of AARCO's area, recorded and shaded

def test_a_search_of_aarcos_area_that_missed_the_north_is_shaded_on_the_map(filling, monkeypatch):  # noqa: F811
    monkeypatch.setattr(pipeline, "geocode", lambda location, key: (*config.OWN_COORDS, config.OWN_ADDRESS))
    monkeypatch.setattr(config, "FILL_IN_SECONDS", 0.3)
    monkeypatch.setattr(osm, "request_json", _servers(fail_times=10_000))
    client = web.create_app().test_client()
    job = client.post("/search", data={"location": config.OWN_ADDRESS, "radius": "30"}).get_json()["job_id"]
    for _ in range(400):
        body = client.get(f"/status/{job}").get_json()
        if body["state"] != "running" and not fillin.running:
            break
        time.sleep(0.05)
    record = client.get("/searches").get_json()["searches"][0]
    assert record["center"] == [round(c, 5) for c in config.OWN_COORDS]
    assert record["map_areas"]["missing"] and record["fill"]["state"] == "gave_up"
    assert len(record["fill"]["boxes"]) >= 3
    data = _data(client)
    statuses = _statuses(data)
    north = [a.name for a in area_map.areas() if a.box[0] > 40.8]
    assert len(north) == 3 and {statuses.pop(n) for n in north} == {"missed"}
    assert set(statuses.values()) == {"searched"}
    assert data["coverage"].endswith(": 6 of the 9 areas searched in full.")


@pytest.mark.parametrize("value", [None, "x", [1], [1, 2, 3]])
def test_a_record_with_an_odd_centre_or_radius_is_not_aarcos(value):
    assert not area_map._of_aarcos_area(_aarcos(center=value, miles=None))
    assert not area_map._of_aarcos_area(_aarcos(radius=value if value != [1] else "abc"))


# ---- the Find page and the Map page say the same about the areas

def _today_of_aarcos_area(**info):
    """Today's search of AARCO's area, as the search history keeps it."""
    day, _ = daily.claim({"location": config.OWN_ADDRESS, "radius": config.SERVICE_AREA_MILES, "source": "auto",
                          "place": f"{config.OWN_COMPANY}, {config.OWN_ADDRESS}", "miles": 0.0,
                          "center": list(AARCO)})
    daily.finish(day, {"leads": 40, "new": 40, "partial": True,
                       "details": {"osm areas searched": "about 7 of 9 areas"}, **info})
    return day


def test_the_find_page_and_the_map_count_and_name_the_areas_alike():
    """After a partial search the Find page's status and the Map page give one count and the
    same names: the center's area by its town (Salt Lake City), never "the search's centre",
    even when only a quarter of it is missing."""
    centre = next(a for a in area_map.areas() if a.n == 1)
    layton = _areas()["Layton"]
    south, west, north, east = centre.box
    quarter = [south, west, (south + north) / 2, (west + east) / 2]
    missing = [quarter, list(layton.box)]
    _today_of_aarcos_area(map_areas={"missing": missing},
                          fill={"state": "filling", "left": 1, "areas": 9, "boxes": missing, "found": 0, "new": 0,
                                "rounds": 0, "where": "the area around the search's centre and Layton",
                                "until": time.time() + 600})
    client = web.create_app().test_client()
    find = client.get("/searches").get_json()["current"]["coverage"]
    shown = _data(client)
    assert centre.name == "Salt Lake City"
    assert find["text"].startswith("Covered so far: 7 of the 9 areas within 30 miles searched in full; partly "
                                   "searched: Salt Lake City. Still filling in, in the background")
    assert ": Salt Lake City and Layton. " in find["text"]
    assert find["short"].startswith("7 of 9 areas searched in full so far; partly searched: Salt Lake City; "
                                    "filling in Salt Lake City and Layton")
    assert shown["coverage"].endswith(": 7 of the 9 areas searched in full, the rest still being asked in the "
                                      "background.")
    statuses = _statuses(shown)
    assert statuses.pop("Salt Lake City") == "partly" and statuses.pop("Layton") == "asking"
    assert set(statuses.values()) == {"searched"}
    for text in (find["text"], find["short"], shown["coverage"], *(a["name"] for a in shown["areas"])):
        assert "centre" not in text and "north-west" not in text, text


def _named(text, label):
    """The area names a coverage text lists after `label` ("partly searched: ")."""
    if label not in text:
        return set()
    said = text.split(label, 1)[1].split(".", 1)[0].split(";", 1)[0].split(" (", 1)[0]
    return {_name.strip() for chunk in said.split(", ") for _name in chunk.split(" and ")}


@pytest.mark.parametrize("state", ["stopped", "new search", "gave_up", "interrupted", "filling", "stopping"])
def test_a_partly_searched_area_is_never_called_not_searched(state, monkeypatch):
    """Three of the four quarters of the center's area (Salt Lake City) came in, and none
    of Layton: in every state of the filling in, the Find page's status and its Details
    line name Salt Lake City as partly searched and only Layton as not searched, as the
    Map page shades them."""
    centre, layton = next(a for a in area_map.areas() if a.n == 1), _areas()["Layton"]
    south, west, north, east = centre.box
    mid_lat, mid_lon = (south + north) / 2, (west + east) / 2
    quarter = [mid_lat, mid_lon, north, east]         # the one quarter that didn't come in
    missing = [quarter, list(layton.box)]
    fill = {"state": "filling", "left": 2, "areas": 9, "boxes": missing, "found": 0, "new": 0, "rounds": 2,
            "where": "Salt Lake City and Layton", "until": time.time() + 600}
    if state in ("stopped", "new search"):
        fill.update(state="stopped", why=fillin.PAUSED if state == "stopped" else fillin.NEW_SEARCH)
    elif state == "gave_up":
        fill["state"] = "gave_up"
    elif state == "interrupted":                       # a restart cut it short (daily reads it so)
        fill["until"] = time.time() - daily.FILL_GRACE_SECONDS - 5
    elif state == "stopping":                          # searching was paused: it is stopping
        monkeypatch.setenv(config.SEARCH_PAUSED_ENV, "1")
    _today_of_aarcos_area(map_areas={"missing": missing}, fill=fill)
    client = web.create_app().test_client()
    current = client.get("/searches").get_json()["current"]
    find = current["coverage"]
    shown = _statuses(_data(client))
    assert shown.pop("Salt Lake City") == "partly" and shown.pop("Layton") in ("missed", "asking")
    assert set(shown.values()) == {"searched"}
    for text in (find["text"], find["short"]):
        assert _named(text, "partly searched: ") == {"Salt Lake City"}, text
        assert "Salt Lake City didn't come in" not in text and "these weren't searched" not in text
    assert find["text"].startswith("Covered so far: " if state in ("filling", "stopping") else "Covered: ")
    assert find["text"].count("Salt Lake City") == (2 if state in ("filling", "stopping") else 1)
    assert "7 of the 9 areas within 30 miles searched in full; partly searched: Salt Lake City" in find["text"]
    if state in ("filling", "stopping"):
        # Still being filled in: both are named among the areas being filled in, as the Map
        # page says (partly searched, the rest still filling in; not searched yet).
        assert find["filling"] and "Salt Lake City and Layton" in find["text"]
    else:
        assert find["state"] == ("stopped" if state == "new search" else state)
        for text in (find["text"], find["short"]):
            assert _named(text, "not searched: ") == {"Layton"}, text
        statuses = _statuses(_data(client))
        for name in _named(find["text"], "partly searched: "):
            assert statuses[name] == area_map.PARTLY
        for name in _named(find["text"], "not searched: "):
            assert statuses[name] == area_map.MISSED
    assert dict(current["details"])["Free map data: area covered"] == find["short"]


def test_the_problem_report_never_calls_a_partly_searched_area_missing(monkeypatch):
    """When the filling in gives up, the administrator's problem report says the areas
    left came in only in part, or not at all, when some of them partly came in."""
    from leadgen import alerts
    sent: list[str] = []
    monkeypatch.setattr(alerts, "report", lambda kind, text, **kw: sent.append(text))
    day = _today_of_aarcos_area()
    fill = {"state": "gave_up", "left": 2, "areas": 9, "where": "Salt Lake City and Layton"}
    fillin._finish(day, fill, 0, area_map.Tally(9, 7, ("Salt Lake City", "Layton"), ("Salt Lake City",)))
    assert sent == ["Today's search stayed incomplete: 2 of the 9 areas (around Salt Lake City and Layton) came in "
                    "only in part, or not at all, from the free map data today, so businesses there are missing "
                    "until the next search."]


def test_a_record_an_earlier_version_saved_still_reads_alike_on_both_pages():
    """A search saved before the missing parts were kept named the center's area "the area
    around the search's centre": both pages now name it Salt Lake City, and the record keeps
    its words."""
    layton = _areas()["Layton"]
    day = _today_of_aarcos_area(fill={"state": "gave_up", "left": 2, "areas": 9, "found": 0, "new": 0, "rounds": 3,
                                      "where": "the area around the search's centre and Layton"})
    client = web.create_app().test_client()
    find = client.get("/searches").get_json()["current"]["coverage"]
    assert "; not searched: Salt Lake City and Layton. The rest didn't come in" in find["text"]
    assert "centre" not in find["text"]
    statuses = _statuses(_data(client))
    assert statuses.pop("Salt Lake City") == statuses.pop(layton.name) == "missed"
    assert set(statuses.values()) == {"searched"}
    assert daily.info(day)["fill"]["where"] == "the area around the search's centre and Layton"
