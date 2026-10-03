"""Once-a-day searches, the call log, the stats page and search progress."""

import time

import pytest

from leadgen import calls, daily, marks, saved, stats, web
from leadgen.geo import GeocodeError
from leadgen.models import Lead
from leadgen.pipeline import RunResult
from leadgen.progress import LOCATE, MAP, PAID, SAVE, Step


def _lead(name, source_id, tier="A", score=70, **kw):
    return Lead(name=name, lat=40.75 + len(name) * 0.01, lon=-111.9, source="osm",
                source_id=source_id, tier=tier, score=score, **kw)


def _wait(client, job):
    for _ in range(200):
        body = client.get(f"/status/{job}").get_json()
        if body["state"] != "running":
            return body
        time.sleep(0.05)
    raise AssertionError("the search never finished")


def test_find_leads_works_once_per_calendar_day(monkeypatch):
    monkeypatch.setattr(web.finding, "run", lambda params, progress: RunResult(
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
    tomorrow = client.post("/search", data={"location": "84101"})
    assert tomorrow.status_code == 200
    assert _wait(client, tomorrow.get_json()["job_id"])["state"] == "done"


def test_a_failed_search_gives_the_day_back(monkeypatch):
    def fail(params, progress):
        raise GeocodeError("Could not find 'nowhere'")
    monkeypatch.setattr(web.finding, "run", fail)
    client = web.create_app().test_client()
    job = client.post("/search", data={"location": "84101"}).get_json()["job_id"]
    assert _wait(client, job)["state"] == "error"
    assert not client.get("/searches").get_json()["used_today"]


def test_a_place_that_cant_be_found_is_refused_before_the_day_is_claimed(monkeypatch):
    from leadgen import geo
    monkeypatch.setattr(geo, "request_json", lambda *a, **k: [])
    started = []
    monkeypatch.setattr(web.finding, "run", lambda *a: started.append(1))
    client = web.create_app().test_client()
    res = client.post("/search", data={"location": "nowhere zz"})
    assert res.status_code == 400 and res.get_json()["field"] == "location"
    assert "Could not find the place 'nowhere zz'" in res.get_json()["error"]
    assert not started and not client.get("/searches").get_json()["used_today"]
    assert daily.history()["searches"] == []                # not even a failed attempt


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
    first = client.post("/calls", json={"by": "Sam", "key": lead.uid, "outcome": "Follow Up",
                                        "notes": "Talked to the manager; call back Friday."})
    assert first.get_json()["ok"]
    client.post("/calls", json={"by": "Sam", "key": lead.uid, "outcome": "Interested", "notes": "Wants a quote"})
    row = client.get("/leads").get_json()["leads"][0]
    assert row["call_outcome"] == "Interested" and row["call_count"] == 2
    assert row["call_notes"] == "Wants a quote" and row["last_call"]
    history = client.get(f"/calls/{lead.uid}").get_json()["calls"]
    assert [c["outcome"] for c in history] == ["Interested", "Follow Up"]
    csv = client.get("/download/saved.csv").data.decode("utf-8-sig")
    assert "Interested" in csv and "Wants a quote" in csv


def test_a_call_sent_twice_is_recorded_once():
    """The page gives each call its own id; the same call sent twice at once (a retry
    on a flaky connection) is one call in the history."""
    import threading
    import uuid
    lead = _lead("Costco", "c")
    saved.save_search([lead])
    app = web.create_app()
    call_id = uuid.uuid4().hex
    answers = []

    def send():
        answers.append(app.test_client().post("/calls", json={"by": "Sam",
            "key": lead.uid, "outcome": "Follow Up", "notes": "Call back Friday", "id": call_id}))
    threads = [threading.Thread(target=send) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert [a.status_code for a in answers] == [200, 200]
    assert {a.get_json()["call"]["id"] for a in answers} == {call_id}
    assert len(calls.history(lead.uid)) == 1
    client = app.test_client()
    assert client.post("/calls", json={"by": "Sam", "key": lead.uid, "outcome": "Interested",
                                       "id": "x"}).status_code == 400
    # Undone, and then a late copy arrives: it stays undone.
    assert client.post("/calls/undo", json={"id": call_id}).get_json()["ok"]
    late = client.post("/calls", json={"by": "Sam", "key": lead.uid, "outcome": "Follow Up", "id": call_id})
    assert late.status_code == 400 and "already undone" in late.get_json()["error"]
    assert calls.history(lead.uid) == []


def test_call_input_is_checked():
    lead = _lead("Costco", "c")
    saved.save_search([lead])
    client = web.create_app().test_client()
    assert client.post("/calls", json={"by": "Sam", "key": lead.uid, "outcome": "Maybe"}).status_code == 400
    assert client.post("/calls", json={"by": "Sam", "key": "nope", "outcome": "Interested"}).status_code == 400
    assert client.post("/calls", json={"by": "Sam", "key": lead.uid, "outcome": "Interested"},
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


def test_competitors_and_own_listing_are_left_out_of_stats_and_checking():
    """Competitors and AARCO's own listing stay in the list, flagged, but are not
    prospects: marking one Yes changes no Stats figure and no "to check" count."""
    from leadgen import config
    from leadgen.scoring import score_lead
    leads = [Lead(name=n, lat=40.7 + i / 100, lon=-111.9, source="osm", source_id=f"n{i}",
                  raw_categories=["shop=supermarket"]) for i, n in
             enumerate(["Harmons", "Smith's Marketplace", "Pro Baler", "AARCO Compactor"])]
    for lead in leads:
        score_lead(lead, config.DEFAULT_KEYWORDS)
    assert [l.lead_type for l in leads[2:]] == ["Competitor", "Own company"]
    saved.save_search(leads, config.DEFAULT_KEYWORDS)
    marks.set_mark(leads[0].uid, "yes")
    marks.set_mark(leads[1].uid, "no")
    client = web.create_app().test_client()
    before = client.get("/stats").get_json()
    counts = client.get("/leads?tab=unchecked&limit=10").get_json()["counts"]
    marks.set_mark(leads[2].uid, "yes")                # a competitor does run balers...
    marks.set_mark(leads[3].uid, "no")
    after = client.get("/stats").get_json()
    for key in ("with_equipment", "checked", "average_score", "average_tier", "by_tier"):
        assert after[key] == before[key], key         # ...but it isn't a prospect
    assert after["left_out"] == 2 and after["with_equipment"] == 1 and after["checked"] == 2
    page = client.get("/leads?tab=unchecked&limit=10").get_json()
    assert page["counts"] == counts and page["counts"]["unchecked"] == 0
    assert page["counts"]["competitors"] == 2 and page["counts"]["all"] == 4
    listed = client.get("/leads?tab=competitors&limit=10").get_json()["leads"]
    assert {l["name"] for l in listed} == {"Pro Baler", "AARCO Compactor"}   # still shown


def test_the_page_gets_one_view_at_a_time():
    """The Leads page asks for the rows it shows: one tab, filtered and sorted, the
    first `limit` of them, plus every tab's count."""
    leads = [_lead(f"Store {i:02}", f"s{i}", score=20 + i, city="Ogden" if i % 2 else "Sandy")
             for i in range(30)]
    saved.save_search(leads)
    marks.set_mark(leads[0].uid, "yes")
    client = web.create_app().test_client()
    page = client.get("/leads?tab=unchecked&limit=5").get_json()
    assert page["total"] == 29 and len(page["leads"]) == 5
    scores = [l["score"] for l in page["leads"]]
    assert scores == sorted(scores, reverse=True)
    assert page["counts"]["unchecked"] == 29 and page["counts"]["yes"] == 1
    page = client.get("/leads?tab=unchecked&limit=3&sort=name&dir=asc&q=ogden").get_json()
    assert page["total"] == 15 and [l["name"] for l in page["leads"]] == ["Store 01", "Store 03", "Store 05"]
    page = client.get(f"/leads?tab=unchecked&limit=3&sort=name&keep={leads[0].uid}").get_json()
    assert page["leads"][0]["name"] == "Store 00"       # a row just marked stays put
    page = client.get("/leads?tab=all&limit=2&sort=name&dir=desc").get_json()
    assert [l["name"] for l in page["leads"]] == ["Store 29", "Store 28"]
    assert client.get("/leads?tab=yes&limit=300").get_json()["total"] == 1
    assert len(client.get("/leads").get_json()["leads"]) == 30          # no view: everything


def test_progress_steps():
    job = {}
    progress = web._Progress(job)
    progress(Step("Locating '84101'", LOCATE))
    assert job["step"] == 0 and job["message"] == "Finding the location"
    progress(Step("Yelp: ... up to 10 calls", PAID, 0, 10, "yelp"))
    for n in range(5):
        progress(Step(f"Yelp page 1: 'q{n}' ({n + 1}/19)", PAID, n + 1, 10, "yelp", f"q{n}"))
    # Half the calls: half of Yelp's share (3 + 47 / 2).
    assert job["step"] == 1 and progress.pct() == pytest.approx(26.5)
    assert job["message"] == "Searching Yelp for q4 (5 of up to 10 calls)"
    progress(Step("OpenStreetMap: (server 1 of 4)", MAP, 0, 1))
    # Yelp's share is now the 5 calls it made: the bar goes on from where it was.
    assert job["step"] == 2 and progress.pct() == pytest.approx(100 * 26.5 / 76.5)
    progress(Step("Saving leads", SAVE, 1, 3))
    assert job["step"] == 4 and progress.pct() == pytest.approx(100 * (26.5 + 36 + 8 + 2) / 76.5)


def test_misclicks_can_be_undone_right_after(monkeypatch):
    lead = _lead("Costco", "c")
    saved.save_search([lead])
    client = web.create_app().test_client()
    undo = client.post("/mark", json={"by": "Sam", "key": lead.uid, "value": "yes"}).get_json()["undo"]
    assert client.post("/mark/undo", json={"id": undo["id"]}).get_json()["ok"]
    assert client.get("/leads").get_json()["leads"][0]["has_baler"] == ""     # back to Not checked
    assert client.post("/mark/undo", json={"id": undo["id"]}).status_code == 409   # only once
    client.post("/mark", json={"by": "Sam", "key": lead.uid, "value": "yes"})
    undo = client.post("/mark", json={"by": "Sam", "key": lead.uid, "value": "no"}).get_json()["undo"]
    client.post("/mark/undo", json={"id": undo["id"]})
    assert client.get("/leads").get_json()["leads"][0]["has_baler"] == "yes"
    first = client.post("/calls", json={"by": "Sam", "key": lead.uid, "outcome": "Interested"}).get_json()["call"]
    second = client.post("/calls", json={"by": "Sam", "key": lead.uid, "outcome": "Bad Lead"}).get_json()["call"]
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
    undo_a = client.post("/mark", json={"by": "Sam", "key": a.uid, "value": "yes"}).get_json()["undo"]
    undo_b = client.post("/mark", json={"by": "Sam", "key": b.uid, "value": "no"}).get_json()["undo"]
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
    old = client.post("/mark", json={"by": "Sam", "key": a.uid, "value": "yes"}).get_json()["undo"]
    client.post("/mark", json={"by": "Sam", "key": a.uid, "value": "no"})
    assert client.post("/mark/undo", json={"id": old["id"]}).status_code == 409
    assert marks.get_all([a.uid]) == {a.uid: "no"}
    # Clicking the mark it already has changes nothing and offers no undo.
    assert client.post("/mark", json={"by": "Sam", "key": a.uid, "value": "no"}).get_json()["undo"] is None


def test_marks_only_for_saved_businesses():
    client = web.create_app().test_client()
    res = client.post("/mark", json={"by": "Sam", "key": "no-such-lead", "value": "yes"})
    assert res.status_code == 404 and "saved list" in res.get_json()["error"]
    from leadgen import store
    with store.connect() as db:
        assert db.all("SELECT uid FROM marks") == []


def test_stats_round_halves_up_as_people_do():
    """5 of 8 is 63% and an average of 62.5 is 63, as on a calculator (Python's round()
    would give 62 for both)."""
    leads = [_lead(f"A{i}", f"a{i}", "A", 70, has_baler="yes" if i < 5 else "no") for i in range(8)]
    tier_a = stats.summarize(leads)["by_tier"][0]
    assert (tier_a["yes"], tier_a["checked"], tier_a["pct"]) == (5, 8, 63)
    two = [_lead("Y1", "y1", "A", 60, has_baler="yes"), _lead("Y2", "y2", "A", 65, has_baler="yes")]
    assert stats.summarize(two)["average_score"] == 63            # (60 + 65) / 2 = 62.5
    assert [stats.ratio(1, 8, 100), stats.ratio(3, 8, 100), stats.ratio(1, 3, 100),
            stats.ratio(2, 3, 100), stats.ratio(0, 4, 100), stats.ratio(4, 4, 100)] == [13, 38, 33, 67, 0, 100]
