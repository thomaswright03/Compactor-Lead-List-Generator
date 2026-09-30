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
