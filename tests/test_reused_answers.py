"""A search of an area searched before: the free map data is asked again the next day
(its answers are kept for less than a day), and a paid source that reuses the answers it
saved says so, with the day of the earlier search, so "0 new" never looks like a search
that didn't look."""

import datetime as dt
import json
import os
import time

from leadgen import config, daily, http, localtime, store
from leadgen.pipeline import SearchParams
from leadgen.sources import osm, paging, yelp
from leadgen.web import finding

YELP_KEY = "y" * 128


def _age_the_cache(hours):
    """Make every saved answer `hours` old."""
    then = time.time() - hours * 3600
    for path in http.CACHE_DIR.glob("*.json"):
        os.utime(path, (then, then))


def _map_servers(monkeypatch):
    asked = []

    def servers(method, url, data, **kw):
        asked.append(data["data"])
        return {"elements": [{"type": "node", "id": 1, "lat": 40.76, "lon": -111.89,
                              "tags": {"name": "Smith's Marketplace", "shop": "supermarket"}}]}
    monkeypatch.setattr(config, "OVERPASS_ENDPOINTS", config.OVERPASS_ENDPOINTS[:1])
    monkeypatch.setattr(osm, "request_json", servers)
    return asked


def test_the_next_days_search_asks_the_map_servers_again(monkeypatch):
    asked = _map_servers(monkeypatch)
    osm.search(40.76, -111.89, 10)
    first = len(asked)
    assert first >= 1
    # Later the same day (a map area asked again, the filling in): the answers are reused.
    _age_the_cache(1)
    osm.search(40.76, -111.89, 10)
    assert len(asked) == first
    # The next day's search of the same area asks the servers again.
    _age_the_cache(13)
    osm.search(40.76, -111.89, 10)
    assert len(asked) == 2 * first
    assert config.OVERPASS_CACHE_TTL_SECONDS < 24 * 3600 <= config.CACHE_TTL_SECONDS


def test_a_saved_answer_is_kept_as_long_as_it_is_asked_to_be(monkeypatch):
    sent = []

    class Answer:
        status_code, text, headers = 200, "{}", {}

        def json(self):
            return {"n": len(sent)}

    def request(method, url, **kw):
        sent.append(url)
        return Answer()
    monkeypatch.setattr(http.requests, "request", request)
    for _ in range(2):
        http.request_json("GET", "https://example.test/a", cache_ttl=3600)
    assert len(sent) == 1
    _age_the_cache(2)
    http.request_json("GET", "https://example.test/a", cache_ttl=3600)
    assert len(sent) == 2
    # Without cache_ttl the week-long default applies.
    _age_the_cache(2)
    http.request_json("GET", "https://example.test/a")
    assert len(sent) == 2


def _fake_yelp(monkeypatch):
    calls = []

    def fake(method, url, *, params=None, headers=None, response_headers=None, **kw):
        calls.append(dict(params))
        return {"total": 3, "businesses": [
            {"id": f"{params.get('term') or params['categories']}-{k}", "name": f"Biz {k}", "review_count": 10,
             "coordinates": {"latitude": 40.76, "longitude": -111.89},
             "location": {"address1": "1 Main St", "city": "Salt Lake City", "state": "UT", "zip_code": "84101"},
             "categories": [{"alias": "grocery", "title": "Grocery"}], "is_closed": False} for k in range(3)]}
    monkeypatch.setattr(yelp, "request_json", fake)
    return calls


def test_a_paid_source_says_when_it_reused_saved_answers(monkeypatch):
    calls = _fake_yelp(monkeypatch)
    _, n, warnings = yelp.search(40.76, -111.89, 20, ["a", "b"], YELP_KEY)
    assert n == 2 and not any(paging.REUSED in w for w in warnings)
    calls.clear()
    _, n, warnings = yelp.search(40.76, -111.89, 20, ["a", "b"], YELP_KEY)
    assert n == 0 and calls == []
    [note] = [w for w in warnings if paging.REUSED in w]
    assert note == ("Yelp reused the answers of all its 2 searches, saved by a search of this area in the last "
                    "7 days (no Yelp calls spent on them): businesses found then count as updated, not new, "
                    "and one Yelp listed since may be missing.")
    # Plain words already: the page shows it as it is.
    assert finding.plain_warning(note) == note
    # One new phrase: only the reused ones are counted.
    _, n, warnings = yelp.search(40.76, -111.89, 20, ["a", "b", "c"], YELP_KEY)
    assert n == 1 and any(w.startswith("Yelp reused the answers of 2 of its 3 searches") for w in warnings)


def _searched(days_ago, place, **info):
    at = time.time() - days_ago * 86400
    day = (localtime.now() - dt.timedelta(days=days_ago)).strftime("%Y-%m-%d")
    with store.connect() as db:
        db.run("INSERT INTO searches (day, at, info) VALUES (?, ?, ?)",
               (day, at, json.dumps({"place": place, "leads": 12, "new": 12, **info})))
    return at


def test_the_result_says_when_the_area_was_searched_before():
    place = "Arco Compactor, 876 Fortune Rd, Salt Lake City, UT 84104"
    at = _searched(3, place)
    _searched(1, "Ogden, UT")                     # another area, searched since
    params = SearchParams(location="84104", place=place)
    note = paging.reused_note("Yelp", 2, 2, config.YELP_CACHE_TTL_SECONDS)
    got = finding._when_reused(["Something else.", note], params, daily.today())
    assert got == ["Something else.",
                   f"This area was also searched on {localtime.date_time_text(at)}, and the answers saved then "
                   "were reused where they could be: what was found then counts as updated, not new.", note]
    # Nothing reused, or no earlier search of the area within the week: nothing added.
    assert finding._when_reused(["Something else."], params, daily.today()) == ["Something else."]
    other = SearchParams(location="Provo", place="Provo, UT")
    assert finding._when_reused([note], other, daily.today()) == [note]


def test_an_earlier_search_older_than_the_saved_answers_is_not_named():
    place = "Murray, UT"
    _searched(9, place)
    assert daily.earlier_search(place, daily.today(), time.time() - config.CACHE_TTL_SECONDS) is None
    _searched(2, place, leads=0)
    found = daily.earlier_search(place, daily.today(), time.time() - config.CACHE_TTL_SECONDS)
    assert found is not None and found["leads"] == 0
