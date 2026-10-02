"""The emergency switches work from inside the site, with no restart."""

import pytest

from leadgen import config, pipeline, switches, web
from leadgen.pipeline import SearchParams

# ---- the emergency switches work from inside the site, with no restart


def test_pausing_in_the_site_stops_the_next_search():
    client = web.create_app().test_client()
    assert client.post("/switches", json={"key": "search_paused", "on": True, "by": "Tom"}).status_code == 200
    refused = client.post("/search", data={"location": "84101"})
    assert refused.status_code == 503 and "Searching is paused" in refused.get_json()["error"]
    listed = client.get("/searches").get_json()
    assert listed["paused"] and listed["switches"]["search_paused"]["by"] == "Tom"
    assert listed["switches"]["search_paused"]["when"]
    assert config.stop_reason() == "the administrator paused searching"   # a running search stops
    client.post("/switches", json={"key": "search_paused", "on": False})
    assert client.get("/searches").get_json()["paused"] is None
    assert config.stop_reason() is None


def test_paid_sources_can_be_switched_off_in_the_site():
    client = web.create_app().test_client()
    client.post("/switches", json={"key": "yelp_off", "on": True})
    assert config.stop_reason("yelp") == "the administrator switched Yelp off"
    assert config.stop_reason("google") is None
    with pytest.raises(pipeline.PipelineError, match="Yelp searches are switched off"):
        pipeline.run(SearchParams(source="yelp", yelp_api_key="k"))


def test_the_environment_switch_stays_as_a_backup(monkeypatch):
    monkeypatch.setenv(config.SEARCH_PAUSED_ENV, "1")
    client = web.create_app().test_client()
    client.post("/switches", json={"key": "search_paused", "on": False})
    state = client.get("/searches").get_json()["switches"]["search_paused"]
    assert state["on"] and state["env"]
    assert switches.is_on(config.SEARCH_PAUSED_ENV)


def test_an_unknown_switch_is_refused():
    client = web.create_app().test_client()
    assert client.post("/switches", json={"key": "everything", "on": True}).status_code == 400
    assert client.post("/switches", json={"key": "yelp_off", "on": "yes"}).status_code == 400


# ---- when the switches can't be read, a running search makes no more paid lookups

def _switches_unreadable(monkeypatch):
    def down(*a, **k):
        raise RuntimeError("the database stopped answering")
    monkeypatch.setattr(switches.store, "connect", down)


def test_unreadable_switches_stop_paid_lookups_and_keep_a_pause_seen_on(monkeypatch):
    monkeypatch.setattr(switches, "_known", {})
    client = web.create_app().test_client()
    client.post("/switches", json={"key": "search_paused", "on": True})
    assert config.stop_reason("osm") == "the administrator paused searching"   # read: on
    _switches_unreadable(monkeypatch)
    # The pause last seen on still counts; with nothing known, paid lookups stop.
    assert config.stop_reason("osm") == "the administrator paused searching"
    assert config.stop_reason("google") == "the administrator paused searching"
    monkeypatch.setattr(switches, "_known", {})
    assert config.stop_reason("google") == config.SWITCHES_UNREAD
    assert config.stop_reason("yelp") == config.SWITCHES_UNREAD
    assert config.stop_reason("osm") is None                # the free map data carries on


def test_a_running_search_makes_no_paid_lookup_once_the_switches_cant_be_read(monkeypatch):
    from leadgen.models import Lead
    monkeypatch.setattr(switches, "_known", {})
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "AIzaFAKEKEYFORTESTS000000000000000000")
    monkeypatch.setattr(pipeline, "geocode", lambda location, key: (40.72, -111.9, "Salt Lake City"))
    asked = []
    monkeypatch.setattr(pipeline.google_places, "search", lambda *a, **k: asked.append("google"))
    found = Lead(name="Costco", lat=40.72, lon=-111.9, source="osm", source_id="c",
                 raw_categories=["shop=wholesale"])

    def osm_search(*a, **k):
        return [found], []
    monkeypatch.setattr(pipeline.osm, "search", osm_search)
    real = pipeline.SearchParams.resolved_keys

    def keys_then_down(self):
        got = real(self)                       # the search started while the switches answered,
        _switches_unreadable(monkeypatch)      # then the database stopped answering
        return got
    monkeypatch.setattr(pipeline.SearchParams, "resolved_keys", keys_then_down)
    result = pipeline.run(SearchParams(source="both"))
    assert asked == [] and [l.name for l in result.leads] == ["Costco"]
    assert any(f"Google wasn't searched: {config.SWITCHES_UNREAD}" in w for w in result.warnings)


def test_a_paid_source_stopped_partway_says_why_in_plain_words():
    note = (f"Stopped early: {config.SWITCHES_UNREAD}: 3 Yelp searches not run, 1 had more "
            "result pages.")
    assert web.finding.plain_warning(note) == (f"Yelp was stopped partway: {config.SWITCHES_UNREAD}. "
                                               "The businesses already found were kept.")
