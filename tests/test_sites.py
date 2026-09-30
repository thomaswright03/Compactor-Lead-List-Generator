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
        m.setattr(saved, "site_groups", lambda sites: [])
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
        m.setattr(saved, "site_groups", lambda sites: [])
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
