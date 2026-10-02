"""One site, one lead: the buildings of one complex and the parts of one named site
merge into a single lead, in a search and in the saved list."""

import time

from leadgen import calls, marks, saved, store, web
from leadgen.dedupe import dedupe, merge_sites, site_name
from leadgen.geo import haversine_miles
from leadgen.models import Lead

MILE_LAT = 1 / 69.0          # degrees of latitude in a mile


def _b(name, lat, lon=-111.83, sid=None, phone="", source="osm", **kw):
    return Lead(name=name, lat=lat, lon=lon, source=source, source_id=sid or f"way/{name}{lat}",
                phone=phone, raw_categories=kw.pop("raw_categories", ["building=apartments"]),
                **kw)


def _complex():
    """Shoreline Ridge 825 ... 834: ten buildings 30 to 80 m apart."""
    lat, out = 40.7600, []
    for n in range(10):
        out.append(_b(f"Shoreline Ridge {825 + n}", lat, sid=f"way/{825 + n}"))
        lat += (30 + 5 * n) / 1609.344 * MILE_LAT
    return out


def _spread():
    """One name over a site 0.3 mi across, in three parts."""
    return [_b("Adagio", 40.7000 + k * 0.15 * MILE_LAT, lon=-111.90, sid=f"way/ad{k}")
            for k in range(3)]


def test_site_name_drops_building_numbers_and_part_words():
    assert site_name("Shoreline Ridge 825") == site_name("Shoreline Ridge 834") == "shoreline ridge"
    assert site_name("Salt Lake Community College Westpointe Center") == \
        site_name("Salt Lake Community College - Westpointe Campus")
    assert site_name("343 Apartments") == site_name("Building 2") == ""


def test_a_ten_building_complex_is_one_lead():
    [lead] = dedupe(_complex())
    assert lead.name == "Shoreline Ridge"
    assert len(lead.parts) == 10
    assert "Shoreline Ridge 825" in lead.alt_names and "Shoreline Ridge 834" in lead.alt_names


def test_one_name_over_a_wide_site_is_one_lead():
    parts = _spread()
    assert haversine_miles(parts[0].lat, parts[0].lon, parts[-1].lat, parts[-1].lon) > 0.29
    [lead] = dedupe(parts)
    assert lead.name == "Adagio" and len(lead.parts) == 3


def test_separate_businesses_stay_apart():
    numbered = [_b("343 Apartments", 40.70), _b("525 Apartments", 40.7003)]
    assert len(dedupe(numbered)) == 2
    # The same name a mile apart, or close by with different phone numbers.
    far = [_b("Adagio", 40.70), _b("Adagio", 40.70 + MILE_LAT)]
    assert len(dedupe(far)) == 2
    phones = [_b("Liberty Village 1", 40.70, phone="801-555-0100"),
              _b("Liberty Village 2", 40.7005, phone="801-555-0199")]
    assert len(dedupe(phones)) == 2
    # A site never grows past half a mile across, however many buildings chain up.
    chain = [_b(f"Long Row {n}", 40.70 + n * 0.15 * MILE_LAT, sid=f"n{n}") for n in range(8)]
    for lead in merge_sites(dedupe(chain)):
        lats = [p["lat"] for p in lead.parts] or [lead.lat]
        assert (max(lats) - min(lats)) / MILE_LAT <= 0.5 + 1e-6


def test_a_site_saves_as_one_lead_and_a_later_search_updates_it():
    found = dedupe(_complex() + _spread())
    assert saved.save_search(found) == (2, 0)
    uids = {l.name: l.uid for l in found}
    # Tomorrow's search finds the same buildings, and one more of the complex on its own.
    again = dedupe(_complex() + _spread())
    assert saved.save_search(again) == (0, 2)
    assert {l.name: l.uid for l in again} == uids
    extra = _b("Shoreline Ridge 835", 40.7601, sid="way/835")
    assert saved.save_search([extra]) == (0, 1)
    assert extra.uid == uids["Shoreline Ridge"]
    leads = saved.load()
    assert sorted(l.name for l in leads) == ["Adagio", "Shoreline Ridge"]


def test_saved_duplicates_merge_once_keeping_marks_and_calls(monkeypatch):
    # Saved before sites were merged: three rows for one complex.
    with monkeypatch.context() as m:
        m.setattr(saved, "same_site", lambda a, b: False)
        rows = [_b(f"Benchmark Plaza {n}", 40.7200 + k * 0.0004, sid=f"way/b{n}")
                for k, n in enumerate((820, 821, 822))]
        for lead in rows:
            saved.save_search([lead])
    first, second, third = (l.uid for l in rows)
    assert len({first, second, third}) == 3
    marks.set_mark(first, "yes", by="Dana")
    time.sleep(0.01)
    marks.set_mark(second, "no", by="Sam")         # newer, but a Yes on one building wins
    calls.log_call(third, "Follow Up", "Call back Monday", by="Dana")

    assert saved.merge_sites() == 2
    assert saved.merge_sites() == 0                # once only
    [lead] = calls.apply(marks.apply(saved.load()))
    assert lead.uid == first and lead.name == "Benchmark Plaza"
    assert lead.has_baler == "yes" and lead.marked_by == "Dana" and lead.mark_clicks == 2
    assert lead.marks_disagreed                    # the row says the marks disagreed
    assert lead.call_count == 1 and lead.call_outcome == "Follow Up"
    assert [m["value"] for m in marks.history(first)] == ["no", "yes"]
    with store.connect() as db:
        merged = db.all("SELECT uid, into_uid FROM merged_leads ORDER BY uid")
    assert sorted(merged) == sorted([(second, first), (third, first)])
    # A later search of one of the merged buildings finds the merged lead.
    again = _b("Benchmark Plaza 822", 40.7208, sid="way/b822")
    saved.save_search([again])
    assert again.uid == first
    # The History box shows both marks.
    body = web.create_app().test_client().get(f"/calls/{first}").get_json()
    assert [m["by"] for m in body["marks"]] == ["Sam", "Dana"]
    assert body["calls"][0]["outcome"] == "Follow Up"


def test_extra_search_words_start_empty_and_add_to_the_standard_words():
    from leadgen import config
    from leadgen.web.finding import parse_form

    page = web.create_app().test_client().get("/").get_data(as_text=True)
    assert '<input name="keywords" value=""' in page
    assert "are always searched" in page
    assert parse_form({"location": "84104"}).keywords == config.DEFAULT_KEYWORDS
    assert parse_form({"location": "84104", "keywords": "pallets, Baler"}).keywords == \
        config.DEFAULT_KEYWORDS + ["pallets"]


def test_utah_time_zone_data_is_always_there(monkeypatch, caplog):
    import zoneinfo

    from leadgen import localtime

    assert localtime.UTAH is not None and str(localtime.UTAH) == "America/Denver"
    requirements = (__import__("pathlib").Path(__file__).parent.parent / "requirements.txt").read_text()
    assert "\ntzdata==" in requirements
    # Without the system's zone data, the pinned tzdata package still gives Utah time.
    zoneinfo.reset_tzpath(to=[])
    zoneinfo.ZoneInfo.clear_cache()
    try:
        try:
            import tzdata  # noqa: F401
        except ImportError:
            # Not installed here (it is on the server): the fallback must be logged.
            assert localtime._load_utah() is None
            assert "No time zone data" in caplog.text
        else:
            assert str(localtime._load_utah()) == "America/Denver"
    finally:
        zoneinfo.reset_tzpath()
        zoneinfo.ZoneInfo.clear_cache()


def test_a_rerun_asks_only_for_the_map_areas_still_missing(monkeypatch):
    """One part of a 30-mile search fails: the first run says how much was covered;
    the re-run asks only for that part (the rest answered and is kept), and is complete."""
    import re

    import pytest

    from leadgen import config
    from leadgen.http import HttpError
    from leadgen.sources import osm

    def box_of(query):
        m = re.search(r"\(around:[^)]*\)\(([-\d.]+),([-\d.]+),([-\d.]+),([-\d.]+)\)", query)
        return tuple(map(float, m.groups()))

    asked, broken, down = [], [], [True]

    def servers(method, url, data, **kw):
        box = box_of(data["data"])
        asked.append(box)
        if not broken:
            broken.append(box)                      # the first part asked is the one that fails
        b = broken[0]
        inside = b[0] <= box[0] and box[2] <= b[2] and b[1] <= box[1] and box[3] <= b[3]
        if down[0] and inside:
            raise HttpError(f"{url} returned HTTP 504")
        lat, lon = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        return {"elements": [{"type": "node", "id": int(lat * 1e5) + int(-lon * 1e5), "lat": lat,
                              "lon": lon, "tags": {"name": "Market", "shop": "supermarket"}}]}
    monkeypatch.setattr(osm, "request_json", servers)
    monkeypatch.setattr(config, "OVERPASS_ENDPOINTS", config.OVERPASS_ENDPOINTS[:1])
    with pytest.raises(osm.PartialResult) as got:
        osm.search(40.76, -111.89, 30)
    assert got.value.coverage == "about 8 of 9 areas"
    assert len(got.value.leads) == 8
    # The re-run: the servers answer now; only the missing part is asked for.
    asked.clear()
    down[0] = False
    leads, _ = osm.search(40.76, -111.89, 30)
    assert asked == [broken[0]]
    assert len(leads) == 9


def test_has_phone_filter_and_phone_first_among_equal_scores():
    from leadgen import config
    from leadgen.scoring import score_lead

    leads = []
    for i, (name, phone) in enumerate([("Alpha Grocery", ""), ("Beta Grocery", "(801) 555-0101"),
                                       ("Gamma Grocery", "")]):
        lead = Lead(name=name, lat=40.72, lon=-111.9 + i * 0.001, source="yelp",
                    source_id=f"p{i}", phone=phone, raw_categories=["yelp:grocery"])
        score_lead(lead, config.DEFAULT_KEYWORDS)
        leads.append(lead)
    saved.save_search(leads, config.DEFAULT_KEYWORDS)
    client = web.create_app().test_client()
    body = client.get("/leads?tab=unchecked&limit=300").get_json()
    assert body["leads"][0]["name"] == "Beta Grocery"
    body = client.get("/leads?tab=unchecked&limit=300&phone=1").get_json()
    assert [l["name"] for l in body["leads"]] == ["Beta Grocery"] and body["total"] == 1
    assert body["counts"]["unchecked"] == 3             # the tab counts ignore the filter


def _two_buildings(monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(saved, "same_site", lambda a, b: False)
        rows = [_b(f"Granite Yard {n}", 40.7300 + k * 0.0004, sid=f"way/g{n}")
                for k, n in enumerate((830, 831))]
        for lead in rows:
            saved.save_search([lead])
    return [l.uid for l in rows]


def test_merged_buildings_marked_yes_then_no_stay_yes_and_are_flagged(monkeypatch):
    older, newer = _two_buildings(monkeypatch)
    marks.set_mark(older, "yes", by="Dana")
    time.sleep(0.01)
    marks.set_mark(newer, "no", by="Sam")
    assert saved.merge_sites() == 1
    client = web.create_app().test_client()
    body = client.get("/leads?view=yes").get_json()
    [row] = [l for l in body["leads"] if l["key"] == older]
    assert row["has_baler"] == "yes" and row["marks_disagreed"] and row["marked_by"] == "Dana"
    history = client.get(f"/calls/{older}").get_json()["marks"]
    assert [(m["value"], m["by"]) for m in history] == [("no", "Sam"), ("yes", "Dana")]
    # Pressing Yes again confirms it: recorded, and the flag goes.
    undo = client.post("/mark", json={"key": older, "value": "yes", "by": "Lee"}).get_json()["undo"]
    assert undo is not None
    [lead] = marks.apply(saved.load([older]))
    assert lead.has_baler == "yes" and not lead.marks_disagreed and lead.marked_by == "Lee"
    # Undoing the confirmation brings the flag back; a plain repeat click changes nothing.
    assert client.post("/mark/undo", json={"id": undo["id"]}).get_json()["ok"]
    assert marks.apply(saved.load([older]))[0].marks_disagreed
    marks.set_mark(older, "no", by="Lee")
    assert marks.set_mark(older, "no", by="Lee") is None


def test_merged_buildings_that_agree_are_not_flagged(monkeypatch):
    a, b = _two_buildings(monkeypatch)
    marks.set_mark(a, "no", by="Dana")
    marks.set_mark(b, "no", by="Sam")
    assert saved.merge_sites() == 1
    [lead] = marks.apply(saved.load([a]))
    assert lead.has_baler == "no" and not lead.marks_disagreed


# The two shapes the round-16 review found saved twice, from the map data of a 30-mile
# search around Arco's shop: Smith's distribution complex in Layton (a building and the
# yard around it, the yard's name with the town written in) and Hill Air Force Base (the
# airfield and the base, 0.88 miles apart).
def _smiths(outlines=True):
    return [
        _b("Smith's Distribution Center", 41.06668, lon=-111.98468, sid="way/34196828",
           raw_categories=["building=commercial"],
           outline=[41.0661079, -111.9863386, 41.0672478, -111.9830183] if outlines else None),
        _b("Smith's Layton Distribution", 41.06667, lon=-111.98311, sid="way/1488860201",
           raw_categories=["industrial=distributor", "landuse=industrial"],
           outline=[41.0626145, -111.9878035, 41.0707216, -111.9784123] if outlines else None),
    ]


def _hill(outlines=True):
    return [
        _b("Hill Air Force Base", 41.12380, lon=-111.97437, sid="way/1220610287",
           raw_categories=["aeroway=aerodrome", "landuse=military", "military=airfield"],
           outline=[41.1038713, -111.9912949, 41.1437364, -111.9574462] if outlines else None),
        _b("Hill Air Force Base", 41.13414, lon=-111.98416, sid="relation/15541642",
           raw_categories=["landuse=military", "military=base"],
           outline=[41.1032595, -112.024215, 41.1650181, -111.9440702] if outlines else None),
    ]


def test_one_complex_named_with_and_without_its_town_is_one_lead():
    a, b = _smiths()
    assert 0.07 < haversine_miles(a.lat, a.lon, b.lat, b.lon) < 0.09
    for outlines in (True, False):        # rows saved before outlines were kept have none
        [lead] = dedupe(_smiths(outlines))
        assert {p["source_id"] for p in lead.parts} == {"way/34196828", "way/1488860201"}


def test_one_large_site_found_twice_is_one_lead():
    a, b = _hill()
    assert 0.85 < haversine_miles(a.lat, a.lon, b.lat, b.lon) < 0.9
    for outlines in (True, False):
        [lead] = dedupe(_hill(outlines))
        assert lead.name == "Hill Air Force Base" and len(lead.parts) == 2


def test_neighbours_that_are_different_businesses_stay_apart():
    # Two stores in one strip mall, inside the mall's outline.
    mall = [_b("Layton Crossing", 41.0800, lon=-111.9700, sid="way/mall",
               raw_categories=["landuse=retail"], outline=[41.0790, -111.9715, 41.0810, -111.9685]),
            _b("Smith's Marketplace", 41.0801, lon=-111.9702, sid="way/s",
               raw_categories=["shop=supermarket"]),
            _b("Walgreens", 41.0803, lon=-111.9699, sid="way/w", raw_categories=["shop=chemist"])]
    assert len(dedupe(mall)) == 3
    # Two towns' branches side by side; one word left once the town is taken out.
    assert len(dedupe([_b("Layton Auto Parts", 41.0600, sid="n1"),
                       _b("Kaysville Auto Parts", 41.0603, sid="n2")])) == 2
    assert len(dedupe([_b("Daniel Construction Office", 41.0600, sid="n3"),
                       _b("Construction Center", 41.0603, sid="n4")])) == 2
    # The same store name 0.9 miles apart is two stores: only bases, airports and
    # campuses stretch that far.
    assert len(dedupe([_b("Smith's Marketplace", 41.0600, sid="n5", raw_categories=["shop=supermarket"]),
                       _b("Smith's Marketplace", 41.0730, sid="n6",
                          raw_categories=["shop=supermarket"])])) == 2
    # A same-named place inside an outline, with a different phone number, stays apart.
    base = _hill()[1]
    base.phone = "801-777-1110"
    other = _b("Hill Air Force Base", 41.1500, lon=-111.9800, sid="n7", phone="801-555-0100",
               raw_categories=["landuse=military"])
    assert len(dedupe([base, other])) == 2


def test_a_later_search_finding_another_part_joins_the_saved_row():
    smiths, hill = _smiths(), _hill()
    saved.save_search([smiths[0], hill[1]])
    first = {l.source_id: l.uid for l in (smiths[0], hill[1])}
    # Another day the search finds only the other listing of each.
    saved.save_search([smiths[1]])
    saved.save_search([hill[0]])
    assert smiths[1].uid == first["way/34196828"]
    assert hill[0].uid == first["relation/15541642"]
    assert len(saved.load()) == 2


def _saved_apart(monkeypatch, leads):
    """Save each lead on its own row, as before the search rules joined them."""
    with monkeypatch.context() as m:
        m.setattr(saved, "same_site", lambda a, b: False)
        m.setattr(saved, "is_duplicate", lambda a, b: False)
        for lead in leads:
            saved.save_search([lead])
            time.sleep(0.002)
    return [lead.uid for lead in leads]


def test_merge_sites_lists_first_and_merges_only_on_request(monkeypatch, capsys):
    from leadgen import cli

    neighbour = _b("Walgreens", 41.06670, lon=-111.98400, sid="way/w",
                   raw_categories=["shop=chemist"])
    dc, yard, airfield, base, store_ = _saved_apart(
        monkeypatch, _smiths(outlines=False) + _hill(outlines=False) + [neighbour])
    assert len(saved.load()) == 5
    marks.set_mark(yard, "yes", by="Dana")
    calls.log_call(dc, "Follow Up", "Call back Monday", by="Sam")
    marks.set_mark(store_, "no", by="Sam")

    plan = saved.merge_plan()
    assert [[r.uid for r in group] for group in plan] == [[dc, yard], [airfield, base]]
    assert plan[0][0].calls == 1 and plan[0][1].mark == "yes"

    # Listing changes nothing.
    assert cli.main(["merge-sites"]) == 0
    out = capsys.readouterr().out
    assert "2 businesses saved as more than one row" in out
    assert "Smith's Distribution Center" in out and "+ Smith's Layton Distribution" in out
    assert "marked Yes" in out and "1 call" in out and "Walgreens" not in out
    assert "Nothing was changed. Add --apply" in out
    assert len(saved.load()) == 5
    assert marks.apply(saved.load([yard]))[0].has_baler == "yes"

    assert cli.main(["merge-sites", "--apply"]) == 0
    assert "2 saved leads merged" in capsys.readouterr().out
    leads = {l.uid: l for l in calls.apply(marks.apply(saved.load()))}
    assert set(leads) == {dc, airfield, store_}
    assert leads[dc].has_baler == "yes" and leads[dc].call_count == 1
    assert leads[store_].has_baler == "no" and leads[store_].name == "Walgreens"
    assert {p for p in leads[airfield].alt_names} | {leads[airfield].name} >= {"Hill Air Force Base"}
    with store.connect() as db:
        assert sorted(db.all("SELECT uid, into_uid FROM merged_leads")) == sorted(
            [(yard, dc), (base, airfield)])
    assert cli.main(["merge-sites"]) == 0
    assert "No saved leads to merge" in capsys.readouterr().out


def test_a_search_with_listings_of_two_saved_rows_joins_the_first_and_merges_nothing(monkeypatch):
    dc, yard = _saved_apart(monkeypatch, _smiths())
    marks.set_mark(yard, "yes", by="Dana")
    [both] = dedupe(_smiths())
    saved.save_search([both])
    assert both.uid == dc
    # The other row stays as it was, with its mark, until the owner merges them.
    leads = {l.uid: l for l in marks.apply(saved.load())}
    assert set(leads) == {dc, yard} and leads[yard].has_baler == "yes"
    assert [[r.uid for r in g] for g in saved.merge_plan()] == [[dc, yard]]


def test_a_map_area_keeps_its_outline():
    import pytest

    from leadgen.sources import osm

    bounds = {"minlat": 41.0626145, "minlon": -111.9878035, "maxlat": 41.0707216,
              "maxlon": -111.9784123}
    yard = osm.parse_element({"type": "way", "id": 1488860201, "bounds": bounds, "tags": {
        "name": "Smith's Layton Distribution", "landuse": "industrial"}})
    assert yard.outline == pytest.approx([41.0626145, -111.9878035, 41.0707216, -111.9784123], abs=1e-6)
    node = osm.parse_element({"type": "node", "id": 1, "lat": 41.06, "lon": -111.98,
                              "tags": {"name": "Smith's Marketplace", "shop": "supermarket"}})
    assert node.outline is None
    assert osm._outline({"minlat": 41.1, "minlon": -111.9}) is None
    assert osm._outline(None) is None
