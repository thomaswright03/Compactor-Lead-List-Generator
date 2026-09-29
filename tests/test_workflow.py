"""Once-a-day searches, the call log, the stats page and search progress."""

import time

from leadgen import calls, daily, marks, saved, stats, web
from leadgen.geo import GeocodeError
from leadgen.models import Lead
from leadgen.pipeline import RunResult


def _lead(name, source_id, tier="A", score=70, **kw):
    return Lead(name=name, lat=40.75 + len(name) * 0.01, lon=-111.9, source="osm",
                source_id=source_id, tier=tier, score=score, **kw)


def _wait(client, job):
    for _ in range(100):
        body = client.get(f"/status/{job}").get_json()
        if body["state"] != "running":
            return body
        time.sleep(0.05)


def test_find_leads_works_once_per_calendar_day(monkeypatch):
    monkeypatch.setattr(web, "run", lambda params, progress: RunResult(
        [_lead("Walmart", "w")], (40.76, -111.89), "SLC", ["a note"], {"leads kept": 1}))
    day = {"now": "2026-09-29"}
    monkeypatch.setattr(daily, "today", lambda: day["now"])
    client = web.create_app().test_client()
    job = client.post("/search", data={"location": "84101"}).get_json()["job_id"]
    body = _wait(client, job)
    assert body["state"] == "done" and body["pct"] == 100
    again = client.post("/search", data={"location": "84101"})
    assert again.status_code == 409 and "once a day" in again.get_json()["error"]
    history = client.get("/searches").get_json()
    assert history["used_today"] and history["searches"][0]["leads"] == 1
    assert history["searches"][0]["location"] == "84101"
    assert history["searches"][0]["warnings"] == ["a note"] and history["searches"][0]["when"]
    day["now"] = "2026-09-30"                  # a new calendar day, however soon
    assert client.post("/search", data={"location": "84101"}).status_code == 200


def test_a_failed_search_gives_the_day_back(monkeypatch):
    def fail(params, progress):
        raise GeocodeError("Could not find 'nowhere'")
    monkeypatch.setattr(web, "run", fail)
    client = web.create_app().test_client()
    job = client.post("/search", data={"location": "nowhere"}).get_json()["job_id"]
    assert _wait(client, job)["state"] == "error"
    assert not client.get("/searches").get_json()["used_today"]


def test_an_unfinished_search_frees_the_day_after_a_while(monkeypatch):
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(daily.time, "time", lambda: clock["now"])
    day, _ = daily.claim({"location": "84101"})          # the server restarts; it never finishes
    assert day and daily.claim({"location": "84101"})[0] is None
    assert daily.history()["used_today"]
    clock["now"] += daily.STALE_SECONDS + 1
    assert not daily.history()["used_today"]
    assert daily.claim({"location": "84102"})[0] == day
    daily.finish(day, {"leads": 3})
    clock["now"] += daily.STALE_SECONDS * 10
    assert daily.claim({"location": "84103"})[0] is None   # a finished search holds the day


def test_utah_calendar_day(monkeypatch):
    import datetime as dt
    # 11 pm in Utah on Sep 29 is already Sep 30 in UTC; the day is still the 29th.
    monkeypatch.setattr(daily, "_local_now", lambda: dt.datetime(2026, 9, 29, 23, 0))
    assert daily.today() == "2026-09-29"


def test_calls_are_logged_for_good_and_set_the_outcome():
    lead = _lead("Costco", "c")
    saved.save_search([lead])
    marks.set_mark(lead.uid, "yes")
    client = web.create_app().test_client()
    first = client.post("/calls", json={"key": lead.uid, "outcome": "Follow Up",
                                        "notes": "Talked to the manager; call back Friday."})
    assert first.get_json()["ok"]
    client.post("/calls", json={"key": lead.uid, "outcome": "Interested", "notes": "Wants a quote"})
    row = client.get("/leads").get_json()["leads"][0]
    assert row["call_outcome"] == "Interested" and row["call_count"] == 2
    assert row["call_notes"] == "Wants a quote" and row["last_call"]
    history = client.get(f"/calls/{lead.uid}").get_json()["calls"]
    assert [c["outcome"] for c in history] == ["Interested", "Follow Up"]
    csv = client.get("/download/saved.csv").data.decode("utf-8-sig")
    assert "Interested" in csv and "Wants a quote" in csv


def test_call_input_is_checked():
    lead = _lead("Costco", "c")
    saved.save_search([lead])
    client = web.create_app().test_client()
    assert client.post("/calls", json={"key": lead.uid, "outcome": "Maybe"}).status_code == 400
    assert client.post("/calls", json={"key": "nope", "outcome": "Interested"}).status_code == 400
    assert client.post("/calls", json={"key": lead.uid, "outcome": "Interested"},
                       headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert set(calls.OUTCOMES) == {"Interested", "Follow Up", "Not Interested", "Not Qualified",
                                   "No Contact", "Bad Lead"}


def test_stats():
    leads = [_lead("A1", "1", "A", 80, has_baler="yes"), _lead("A2", "2", "A", 70, has_baler="no"),
             _lead("B1", "3", "B", 50, has_baler="yes"), _lead("C1", "4", "C", 30),
             _lead("D1", "5", "D", 10, has_baler="no")]
    s = stats.summarize(leads)
    assert s["with_equipment"] == 2 and s["checked"] == 4
    assert s["average_score"] == 65 and s["average_tier"] == "A"
    assert [(t["tier"], t["yes"], t["checked"], t["pct"]) for t in s["by_tier"]] == [
        ("A", 1, 2, 50), ("B", 1, 1, 100), ("C", 0, 0, None), ("D", 0, 1, 0)]
    client = web.create_app().test_client()
    assert client.get("/stats").get_json()["with_equipment"] == 0


def test_progress_steps():
    job = {}
    progress = web._Progress(job)
    progress("Locating '84101'")
    assert job["step"] == 0
    progress("Yelp: 19 searches x 1 area(s), up to 10 calls (50 of today's 50 left)")
    for n in range(5):
        progress(f"Yelp page 1: 'q{n}' ({n + 1}/19)")
    assert job["step"] == 1 and 25 < progress.pct() < 30
    progress("OpenStreetMap: querying overpass-api.de")
    assert job["step"] == 2 and 50 <= progress.pct() < 86
    progress("Saving leads")
    assert job["step"] == 4 and progress.pct() == 96


def test_misclicks_can_be_undone_right_after(monkeypatch):
    lead = _lead("Costco", "c")
    saved.save_search([lead])
    client = web.create_app().test_client()
    undo = client.post("/mark", json={"key": lead.uid, "value": "yes"}).get_json()["undo"]
    assert client.post("/mark/undo", json={"id": undo["id"]}).get_json()["ok"]
    assert client.get("/leads").get_json()["leads"][0]["has_baler"] == ""     # back to Not checked
    assert client.post("/mark/undo", json={"id": undo["id"]}).status_code == 409   # only once
    client.post("/mark", json={"key": lead.uid, "value": "yes"})
    undo = client.post("/mark", json={"key": lead.uid, "value": "no"}).get_json()["undo"]
    client.post("/mark/undo", json={"id": undo["id"]})
    assert client.get("/leads").get_json()["leads"][0]["has_baler"] == "yes"
    first = client.post("/calls", json={"key": lead.uid, "outcome": "Interested"}).get_json()["call"]
    second = client.post("/calls", json={"key": lead.uid, "outcome": "Bad Lead"}).get_json()["call"]
    assert client.post("/calls/undo", json={"id": second["undo"]["id"]}).get_json()["ok"]
    row = client.get("/leads").get_json()["leads"][0]
    assert row["call_outcome"] == "Interested" and row["call_count"] == 1
    # Later than a few minutes, a click is kept for good.
    real = time.time
    monkeypatch.setattr(marks.time, "time", lambda: real() + marks.UNDO_SECONDS + 1)
    monkeypatch.setattr(calls.time, "time", lambda: real() + calls.UNDO_SECONDS + 1)
    assert client.post("/mark/undo", json={"id": undo["id"]}).status_code == 409
    assert client.post("/calls/undo", json={"id": first["id"]}).status_code == 409
    assert client.post("/mark/undo", json={"key": lead.uid, "previous": ""}).status_code == 400
    assert client.post("/calls/undo", json={"id": first["id"]},
                       headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403


def test_undo_lasts_five_minutes_per_business_and_never_undoes_a_newer_change(monkeypatch):
    a, b = _lead("Costco", "a"), _lead("Walmart", "b")
    saved.save_search([a, b])
    client = web.create_app().test_client()
    clock = {"now": time.time()}
    monkeypatch.setattr(marks.time, "time", lambda: clock["now"])
    undo_a = client.post("/mark", json={"key": a.uid, "value": "yes"}).get_json()["undo"]
    undo_b = client.post("/mark", json={"key": b.uid, "value": "no"}).get_json()["undo"]
    assert undo_a["until"] - clock["now"] == marks.UNDO_SECONDS
    # The page offers both undos, on each business's row, after a reload too.
    rows = {r["name"]: r for r in client.get("/leads").get_json()["leads"]}
    assert rows["Costco"]["undo_mark"]["id"] == undo_a["id"]
    assert rows["Walmart"]["undo_mark"]["id"] == undo_b["id"]
    clock["now"] += 4 * 60                                   # four minutes later
    assert client.post("/mark/undo", json={"id": undo_a["id"]}).get_json()["ok"]
    rows = {r["name"]: r for r in client.get("/leads").get_json()["leads"]}
    assert rows["Costco"]["has_baler"] == "" and rows["Walmart"]["has_baler"] == "no"
    assert rows["Costco"]["undo_mark"] is None
    clock["now"] += 61                                      # past five minutes for Walmart
    assert client.post("/mark/undo", json={"id": undo_b["id"]}).status_code == 409
    rows = {r["name"]: r for r in client.get("/leads").get_json()["leads"]}
    assert rows["Walmart"]["has_baler"] == "no" and rows["Walmart"]["undo_mark"] is None
    # A colleague's newer click can't be overwritten by undoing an older one.
    old = client.post("/mark", json={"key": a.uid, "value": "yes"}).get_json()["undo"]
    client.post("/mark", json={"key": a.uid, "value": "no"})
    assert client.post("/mark/undo", json={"id": old["id"]}).status_code == 409
    assert marks.get_all([a.uid]) == {a.uid: "no"}
    # Clicking the mark it already has changes nothing and offers no undo.
    assert client.post("/mark", json={"key": a.uid, "value": "no"}).get_json()["undo"] is None


def test_marks_only_for_saved_businesses():
    client = web.create_app().test_client()
    res = client.post("/mark", json={"key": "no-such-lead", "value": "yes"})
    assert res.status_code == 404 and "saved list" in res.get_json()["error"]
    from leadgen import store
    with store.connect() as db:
        assert db.all("SELECT uid FROM marks") == []
