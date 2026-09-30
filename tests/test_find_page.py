"""The Find leads page: form messages in the form's own words, the note when Google and
Yelp aren't set up, and progress in plain words."""

import pytest

from leadgen import daily, pipeline, web


def test_find_leads_says_phones_will_be_few_without_google_or_yelp(monkeypatch):
    page = web.create_app().test_client().get("/").get_data(as_text=True)
    assert "most businesses found will have no phone number" in page
    monkeypatch.setenv("YELP_API_KEY", "fake-yelp-key-for-tests")
    page = web.create_app().test_client().get("/").get_data(as_text=True)
    assert "most businesses found will have no phone number" not in page


# ---- form messages use the form's own words

def test_form_messages_use_the_labels_on_screen():
    client = web.create_app().test_client()
    body = client.post("/search", data={"location": "84101", "radius": "0"}).get_json()
    assert body == {"error": "How far must be between 1 and 100 miles", "field": "radius"}
    body = client.post("/search", data={"location": "84101", "min_score": "200"}).get_json()
    assert body["error"].startswith("The score to leave out weak leads below must be between 0 and 100")
    assert not daily.history()["used_today"]


def test_an_unknown_place_gives_the_same_advice_as_the_hint(monkeypatch):
    from leadgen import geo
    monkeypatch.setattr(geo, "request_json", lambda *a, **k: [])
    with pytest.raises(geo.GeocodeError, match="try a ZIP code, city or street address"):
        geo.geocode("Nowhereville zz", "")
    assert pipeline.geocode is geo.geocode


# ---- progress and downloads in plain words

def test_progress_never_names_servers():
    job = {}
    progress = web._Progress(job)
    progress("OpenStreetMap: searching the free map data (server 1 of 4)")
    assert job["message"] == "Searching the free map data…"
    progress("OpenStreetMap: searching the free map data (server 3 of 4)")
    assert "server" not in job["message"] and "trying another source" in job["message"]
