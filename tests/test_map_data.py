"""The free map data (OpenStreetMap via the Overpass mirrors): asked in parts, parts
that fail asked again in quarters, and parts busy servers missed asked again
automatically, so an ordinary search finishes complete without a manual re-run."""

import re
import threading
import time

import pytest

from leadgen import config, pipeline, web
from leadgen.http import HttpError
from leadgen.models import Lead
from leadgen.progress import MAP, Step
from leadgen.scoring import score_lead
from leadgen.sources import osm


def _box_of(query):
    m = re.search(r"\(around:[^)]*\)\(([-\d.]+),([-\d.]+),([-\d.]+),([-\d.]+)\)", query)
    return tuple(map(float, m.groups())) if m else None


def _place(n, lat, lon):
    return {"type": "node", "id": n, "lat": lat, "lon": lon,
            "tags": {"name": f"Market {n}", "shop": "supermarket"}}


def _answer(box):
    lat, lon = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    return {"elements": [_place(int(lat * 1e5) + int(-lon * 1e5), lat, lon)]}


def _throttled(fail_rounds):
    """Mirrors that refuse every query about the northern third of the area for the
    first `fail_rounds` times each is asked (as a throttling server does), then answer."""
    tries: dict = {}

    def servers(method, url, data, **kw):
        box = _box_of(data["data"])
        if box[0] > 40.8:
            tries[box] = tries.get(box, 0) + 1
            if tries[box] <= fail_rounds:
                raise HttpError(f"{url}: ConnectionResetError(104, 'Connection reset by peer')")
        return _answer(box)
    return servers, tries


def test_areas_that_fail_at_first_are_asked_again_and_the_search_is_complete(monkeypatch):
    monkeypatch.setattr(config, "OVERPASS_ENDPOINTS", config.OVERPASS_ENDPOINTS[:1])
    servers, tries = _throttled(fail_rounds=1)
    monkeypatch.setattr(osm, "request_json", servers)
    said, stats = [], {}
    leads, warnings = osm.search(40.76, -111.89, 30, progress=said.append, stats=stats)
    # The 6 southern parts at once; the 3 northern ones as 12 quarters, asked again.
    assert len(leads) == 6 + 12 and warnings == []
    assert stats == {"osm areas asked again": "12 (all answered)"}
    assert any("asking again for the areas the busy map servers missed" in s for s in said)


def test_a_part_that_never_answers_still_leaves_an_honest_incomplete_result(monkeypatch):
    monkeypatch.setattr(config, "OVERPASS_ENDPOINTS", config.OVERPASS_ENDPOINTS[:1])
    servers, tries = _throttled(fail_rounds=99)
    monkeypatch.setattr(osm, "request_json", servers)
    stats = {}
    with pytest.raises(osm.PartialResult) as got:
        osm.search(40.76, -111.89, 30, stats=stats)
    assert got.value.coverage == "about 6 of 9 areas"
    # Every quarter was asked once in the first round and once in each catch-up round.
    assert set(tries.values()) == {config.OVERPASS_RETRY_ROUNDS + 1} | {1}
    assert stats["osm areas asked again"] == "12 (some never answered)"


def test_the_catch_up_rounds_are_bounded_in_time(monkeypatch):
    clock = {"now": 5000.0}
    monkeypatch.setattr(osm.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(osm.time, "sleep", lambda s: clock.__setitem__("now", clock["now"] + s))
    monkeypatch.setattr(config, "OVERPASS_RETRY_PAUSE_SECONDS", 20)

    def hanging(method, url, **kw):
        clock["now"] += kw["timeout"][1]
        raise HttpError(f"{url}: ReadTimeout")
    monkeypatch.setattr(osm, "request_json", hanging)
    with pytest.raises(osm.SourceError):
        osm.search(40.76, -111.89, 30)
    rounds = config.OVERPASS_RETRY_ROUNDS
    bound = (config.OVERPASS_DEADLINE_SECONDS
             + rounds * (config.OVERPASS_RETRY_PAUSE_SECONDS + config.OVERPASS_RETRY_SECONDS)
             + (rounds + 1) * config.OVERPASS_PART_SECONDS)
    assert clock["now"] - 5000 <= bound <= 15 * 60


def _wait(client, job):
    for _ in range(400):
        body = client.get(f"/status/{job}").get_json()
        if body["state"] != "running":
            return body
        time.sleep(0.05)
    raise AssertionError("the search never finished")


def test_a_search_whose_map_servers_are_throttling_finishes_complete(monkeypatch):
    """The day's search, with some areas refused once: it ends done, complete (not
    incomplete), the day is used, and the details say areas were asked again."""
    monkeypatch.setattr(pipeline, "geocode", lambda location, key: (40.76, -111.89, "Salt Lake City"))
    monkeypatch.setattr(config, "OVERPASS_ENDPOINTS", config.OVERPASS_ENDPOINTS[:1])
    servers, _ = _throttled(fail_rounds=1)
    monkeypatch.setattr(osm, "request_json", servers)
    client = web.create_app().test_client()
    job = client.post("/search", data={"location": "84101", "radius": "30"}).get_json()["job_id"]
    body = _wait(client, job)
    assert body["state"] == "done" and not body.get("note")
    assert not any("part of the area" in w for w in body["warnings"])
    history = client.get("/searches").get_json()
    assert history["used_today"] and not history["searches"][0].get("partial")
    details = dict(history["searches"][0]["details"])
    assert details["Free map data: areas asked again automatically"] == "12 (all answered)"


def _lead(name="Costco", sid="c", **kw):
    kw.setdefault("lat", 40.72)
    kw.setdefault("lon", -111.9)
    kw.setdefault("raw_categories", ["shop=wholesale"])
    lead = Lead(name=name, source="osm", source_id=sid, **kw)
    score_lead(lead, config.DEFAULT_KEYWORDS)
    return lead


# ---- the map data is asked in parts

def test_a_wide_search_is_asked_in_parts_and_a_big_query_is_never_needed(monkeypatch):
    """Every query covering more than one part's size fails (as busy public servers
    do); the 30-mile search still finds the businesses in every part."""
    asked = []

    def servers(method, url, data, **kw):
        box = _box_of(data["data"])
        asked.append(box)
        if box is None or box[2] - box[0] > 0.3:
            raise HttpError(f"{url} returned HTTP 504")
        lat, lon = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        return {"elements": [_place(int(abs(lat * 1e4) + abs(lon * 1e4)), lat, lon)]}
    monkeypatch.setattr(osm, "request_json", servers)
    leads, warnings = osm.search(40.76, -111.89, 30)
    assert len(asked) == 9 and None not in asked and warnings == []
    assert len({l.source_id for l in leads}) == 9


def test_a_failed_part_is_asked_again_in_quarters(monkeypatch):
    first = []

    def servers(method, url, data, **kw):
        box = _box_of(data["data"])
        if box and not first:
            first.append(box)
        if box == first[0]:
            raise HttpError(f"{url} returned HTTP 504")
        lat, lon = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        return {"elements": [_place(int(lat * 1e5) + int(-lon * 1e5), lat, lon)]}
    monkeypatch.setattr(osm, "request_json", servers)
    monkeypatch.setattr(config, "OVERPASS_ENDPOINTS", config.OVERPASS_ENDPOINTS[:1])
    leads, _ = osm.search(40.76, -111.89, 30)
    assert len(leads) == 8 + 4                     # the other 8 parts, and the failed one's quarters


def test_parts_no_server_answers_leave_the_map_data_incomplete(monkeypatch):
    monkeypatch.setattr(config, "OVERPASS_ENDPOINTS", config.OVERPASS_ENDPOINTS[:1])

    def servers(method, url, data, **kw):
        box = _box_of(data["data"])
        if box[0] > 40.8:                           # the northern parts never answer
            raise HttpError(f"{url}: ReadTimeout")
        lat, lon = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        return {"elements": [_place(int(lat * 1e5) + int(-lon * 1e5), lat, lon)]}
    monkeypatch.setattr(osm, "request_json", servers)
    with pytest.raises(osm.PartialResult) as got:
        osm.search(40.76, -111.89, 30)
    assert len(got.value.leads) == 6                # the southern and middle rows of parts
    assert "part of the area (about 33% missing)" in str(got.value)


def test_a_partial_map_result_is_saved_and_the_search_marked_incomplete(monkeypatch):
    monkeypatch.setattr(pipeline, "geocode", lambda location, key: (40.72, -111.9, "Salt Lake City"))

    def partial(*a, **k):
        raise osm.PartialResult("OpenStreetMap answered for only part of the area",
                                [_lead()], [])
    monkeypatch.setattr(pipeline.osm, "search", partial)
    client = web.create_app().test_client()
    job = client.post("/search", data={"location": "84101"}).get_json()["job_id"]
    body = _wait(client, job)
    assert body["state"] == "done" and body["saved"] and body["found"] == 1
    assert "answered for only part of the area" in " ".join(body["warnings"])
    record = client.get("/searches").get_json()["searches"][0]
    assert record["partial"] and "only part of the area" in record["reason"]
    assert [l["name"] for l in client.get("/leads").get_json()["leads"]] == ["Costco"]


def _areas(done, total, note=""):
    return Step(f"OpenStreetMap: searching the free map data ({done} of {total} areas done)", MAP,
                done, total, note=note)


def test_progress_says_how_many_parts_of_the_map_are_done():
    job = {}
    progress = web._Progress(job)
    progress(_areas(3, 9))
    assert job["message"] == "Searching the free map data: 3 of 9 areas done…"
    # Yelp and Google made no calls: the bar is locate, the map data, merge and save.
    assert job["step"] == 2 and job["pct"] == pytest.approx(100 * (3 + 36 * 3 / 9) / 53, abs=0.1)


# ---- the map data's progress only moves forward

def test_map_progress_never_counts_backwards():
    job = {}
    progress = web._Progress(job)
    seen = []
    for msg in [_areas(8, 17), _areas(7, 17), _areas(9, 17, "split"), _areas(10, 17, "split"),
                _areas(9, 17, "split")]:
        progress(msg)
        seen.append(job["message"])
    done = [int(m.split(": ")[1].split(" of ")[0]) for m in seen]
    assert done == sorted(done) == [8, 8, 9, 10, 10]
    assert seen[0] == "Searching the free map data: 8 of 17 areas done…"
    assert seen[-1] == ("Searching the free map data: 10 of 17 areas done (some areas are being "
                        "asked again in smaller parts)…")


def test_the_map_search_reports_parts_done(monkeypatch):
    monkeypatch.setattr(osm, "request_json", lambda *a, **k: {"elements": []})
    said = []
    osm.search(40.76, -111.89, 30, progress=said.append)
    counts = [m for m in said if "areas done" in m]
    assert counts[0].endswith("(0 of 9 areas done)") and counts[-1].endswith("(9 of 9 areas done)")
    numbers = [int(m.rsplit("(", 1)[1].split()[0]) for m in counts]
    assert numbers == sorted(numbers)
    # The page's values are the same numbers: the map step, done of the search's 9 areas.
    assert all(m.step == MAP and m.total == 9 for m in said)
    assert [m.done for m in counts] == numbers


# ---- the nearest areas first; the area count stays in view

def _from_centre(box, lat=40.76, lon=-111.89):
    return osm._distance(lat, lon, box)


def test_the_area_around_the_centre_is_asked_first_then_outwards(monkeypatch):
    monkeypatch.setattr(config, "OVERPASS_PARALLEL", 1)
    monkeypatch.setattr(config, "OVERPASS_ENDPOINTS", config.OVERPASS_ENDPOINTS[:1])
    asked = []

    def servers(method, url, data, **kw):
        asked.append(_box_of(data["data"]))
        return _answer(asked[-1])
    monkeypatch.setattr(osm, "request_json", servers)
    leads, _ = osm.search(40.76, -111.89, 30)
    first = asked[0]
    assert first[0] <= 40.76 <= first[2] and first[1] <= -111.89 <= first[3]
    assert [_from_centre(b) for b in asked] == sorted(_from_centre(b) for b in asked)
    # The first business found (and saved) is the one nearest the centre.
    assert leads[0].lat == pytest.approx((first[0] + first[2]) / 2)


def test_a_failed_area_near_the_centre_is_asked_again_before_the_far_ones(monkeypatch):
    monkeypatch.setattr(config, "OVERPASS_PARALLEL", 1)
    monkeypatch.setattr(config, "OVERPASS_ENDPOINTS", config.OVERPASS_ENDPOINTS[:1])
    centre = osm._parts(40.76, -111.89, 30)[0]
    asked = []

    def servers(method, url, data, **kw):
        box = _box_of(data["data"])
        asked.append(box)
        if box == pytest.approx(centre):
            raise HttpError(f"{url} returned HTTP 504")
        return _answer(box)
    monkeypatch.setattr(osm, "request_json", servers)
    osm.search(40.76, -111.89, 30)
    # The centre fails, so its four quarters go next, before any of the eight around it.
    quarters = asked[1:5]
    assert all(centre[0] - 1e-5 <= q[0] < q[2] <= centre[2] + 1e-5
               and centre[1] - 1e-5 <= q[1] < q[3] <= centre[3] + 1e-5 for q in quarters)
    assert len(asked) == 1 + 4 + 8


def test_the_area_count_stays_on_the_page_until_the_map_step_ends(monkeypatch):
    """With the northern areas refused in the first round, the catch-up round still
    says how many of the 9 areas are done (it used to drop the count)."""
    monkeypatch.setattr(config, "OVERPASS_ENDPOINTS", config.OVERPASS_ENDPOINTS[:1])
    monkeypatch.setattr(config, "OVERPASS_RETRY_PAUSE_SECONDS", 0)
    servers, _ = _throttled(fail_rounds=1)
    monkeypatch.setattr(osm, "request_json", servers)
    job = {"skipped": [1]}
    progress = web._Progress(job)
    shown, bar = [], []

    def follow(msg):
        progress(msg)
        shown.append(job["message"])
        bar.append(progress.pct())
    osm.search(40.76, -111.89, 30, progress=follow)
    assert all(re.search(r": \d+ of 9 areas done", m) for m in shown), shown
    assert any("asking again for the areas the busy map servers missed" in m for m in shown)
    assert shown[-1].startswith("Searching the free map data: 9 of 9 areas done")
    # The bar starts in single digits and follows the count.
    assert bar[0] < 10 and bar == sorted(bar)
    done = [int(m.split(": ")[1].split(" of ")[0]) for m in shown]
    assert done == sorted(done)


def test_the_map_data_alone_starts_the_bar_near_zero(monkeypatch):
    """No Google or Yelp: the bar covers locate, the map data, merge and save only."""
    from leadgen.progress import MAP, Step

    gate = threading.Event()

    def search(lat, lon, radius, keywords=(), progress=None, stats=None, stop=None):
        progress(Step("OpenStreetMap: (0 of 9 areas done)", MAP, 0, 9))
        gate.wait(5)
        progress(Step("OpenStreetMap: (3 of 9 areas done)", MAP, 3, 9))
        return [], []
    monkeypatch.setattr(pipeline.osm, "search", search)
    monkeypatch.setattr(pipeline, "geocode", lambda location, key: (40.76, -111.89, "Salt Lake City"))
    client = web.create_app().test_client()
    job = client.post("/search", data={"location": "84101", "radius": "30"}).get_json()["job_id"]
    try:
        for _ in range(100):
            body = client.get(f"/status/{job}").get_json()
            if body["step"] == 2:
                break
            time.sleep(0.02)
        assert body["skipped"] == [1] and body["pct"] < 10
        assert body["message"] == "Searching the free map data: 0 of 9 areas done…"
    finally:
        gate.set()
    _wait(client, job)


def test_the_missing_areas_are_named_by_their_towns():
    parts = [(box, 1 / 9, 0) for box in osm._parts(40.76, -111.89, 30)]
    assert osm.areas_text(parts[:1], 40.76, -111.89, 30) == "Salt Lake City"
    named = osm.areas_text(parts, 40.76, -111.89, 30)
    assert named.startswith("Salt Lake City, ") and named.endswith(" and more")
    # An area with no town in it is named by where it lies from the centre.
    lake = (41.0, -112.6, 41.2, -112.4)
    assert osm.areas_text([(lake, 0.1, 0)], 40.76, -111.89, 40) == "the area to the north-west"
    assert osm.areas_text([], 40.76, -111.89, 30) == ""


# ---- building labels are left out; OSM-only searches say phones will be few

@pytest.mark.parametrize("name", ["Building B", "Bldg. 3", "BUILDING 12A", "Tower 2", "C Building",
                                  "Suite 100", "Wing C"])
def test_labels_for_part_of_a_complex_are_left_out(name):
    element = {"type": "way", "id": 5, "bounds": {"minlat": 40.7, "maxlat": 40.71,
                                                  "minlon": -111.9, "maxlon": -111.89},
               "tags": {"name": name, "building": "apartments"}}
    assert osm.parse_element(element) is None


@pytest.mark.parametrize("name", ["Tower Records", "Building Supply Co", "Suite Dreams Bakery",
                                  "Harmons", "3M", "The Block Restaurant"])
def test_real_business_names_are_kept(name):
    element = {"type": "node", "id": 6, "lat": 40.7, "lon": -111.9,
               "tags": {"name": name, "shop": "supermarket"}}
    assert osm.parse_element(element).name == name


def test_a_generic_map_name_is_made_descriptive():
    """"Recycling" alone gives a salesperson nothing to look up: the map's operator,
    street or city is added to it."""
    from leadgen.sources.osm import parse_element

    def name(tags):
        return parse_element({"type": "node", "id": 7, "lat": 40.7, "lon": -111.9,
                              "tags": tags}).name
    assert name({"name": "Recycling", "amenity": "recycling",
                 "operator": "Salt Lake County"}) == "Recycling (Salt Lake County)"
    assert name({"name": "Recycling", "amenity": "recycling", "addr:housenumber": "1200",
                 "addr:street": "W 500 S"}) == "Recycling at 1200 W 500 S"
    assert name({"name": "Junkyard", "industrial": "scrap_yard",
                 "addr:city": "Magna"}) == "Junkyard, Magna"
    assert name({"name": "Junkyard", "industrial": "scrap_yard"}) == "Junkyard"
    assert name({"name": "Wasatch Recycling", "amenity": "recycling",
                 "operator": "Wasatch"}) == "Wasatch Recycling"
