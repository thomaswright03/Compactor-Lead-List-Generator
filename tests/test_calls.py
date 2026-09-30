"""Calls and who made each mark and call: a call can be logged on any prospect,
whatever its mark, and every mark and call names the person who made it."""

import csv
import io

from leadgen import calls, config, marks, saved, store, web
from leadgen.export import to_csv_bytes
from leadgen.models import Lead
from leadgen.scoring import score_lead


def _lead(name="Costco", sid="c", **kw):
    kw.setdefault("lat", 40.72)
    kw.setdefault("lon", -111.9)
    kw.setdefault("raw_categories", ["shop=wholesale"])
    lead = Lead(name=name, source="osm", source_id=sid, **kw)
    score_lead(lead, config.DEFAULT_KEYWORDS)
    return lead


# ---- calls can be logged on any prospect, whatever its mark

def test_a_call_on_an_unchecked_business_keeps_it_unchecked():
    lead = _lead()
    saved.save_search([lead])
    client = web.create_app().test_client()
    assert client.post("/calls", json={"by": "Sam", "key": lead.uid, "outcome": "No Contact",
                                       "notes": "Rang twice"}).status_code == 200
    unchecked = client.get("/leads?tab=unchecked").get_json()
    assert [l["key"] for l in unchecked["leads"]] == [lead.uid]
    assert unchecked["leads"][0]["call_outcome"] == "No Contact"
    called = client.get("/leads?tab=called").get_json()
    assert [l["key"] for l in called["leads"]] == [lead.uid]
    assert called["counts"]["outcomes"]["No Contact"] == 1
    rows = list(csv.DictReader(io.StringIO(client.get("/download/saved.csv").data.decode("utf-8-sig"))))
    assert rows[0]["Call Result"] == "No Contact" and rows[0]["Has Baler or Compactor?"] == ""
    assert marks.get_all([lead.uid]) == {} and len(calls.history(lead.uid)) == 1


# ---- who made each mark and call

def test_marks_and_calls_record_who_made_them():
    a, b = _lead(), _lead("Walmart", "w")
    saved.save_search([a, b])
    client = web.create_app().test_client()
    assert client.post("/mark", json={"key": a.uid, "value": "yes", "by": " Dana\n  Smith "}).status_code == 200
    assert client.post("/mark", json={"key": b.uid, "value": "no", "by": "Lee"}).status_code == 200
    call = client.post("/calls", json={"key": a.uid, "outcome": "Follow Up", "notes": "Quote",
                                       "by": "Lee"}).get_json()["call"]
    assert call["by"] == "Lee"
    rows = {l["name"]: l for l in client.get("/leads").get_json()["leads"]}
    assert rows["Costco"]["marked_by"] == "Dana Smith" and rows["Walmart"]["marked_by"] == "Lee"
    assert rows["Costco"]["last_call_by"] == "Lee"
    assert [c["by"] for c in client.get(f"/calls/{a.uid}").get_json()["calls"]] == ["Lee"]
    file = list(csv.DictReader(io.StringIO(to_csv_bytes(
        calls.apply(marks.apply(saved.load()))).decode("utf-8-sig"))))
    by_name = {r["Business Name"]: r for r in file}
    assert by_name["Costco"]["Marked By"] == "Dana Smith" and by_name["Costco"]["Called By"] == "Lee"
    assert by_name["Walmart"]["Called By"] == ""


def _maker(uid):
    [lead] = marks.apply(saved.load([uid]))
    return lead.marked_by


def test_a_mark_switched_by_someone_else_names_them_and_undo_names_the_first():
    lead = _lead()
    saved.save_search([lead])
    marks.set_mark(lead.uid, "yes", "Dana")
    undo = marks.set_mark(lead.uid, "no", "Lee")
    assert _maker(lead.uid) == "Lee"
    assert marks.undo(undo["id"])
    assert _maker(lead.uid) == "Dana"
    # A click with no name (or from before names were kept) names nobody.
    marks.set_mark(lead.uid, "no")
    assert _maker(lead.uid) == ""
    assert store.person("x" * 100) == "x" * store.MAX_NAME and store.person(None) == ""


def test_a_call_sent_twice_keeps_its_maker():
    lead = _lead()
    saved.save_search([lead])
    first = calls.log_call(lead.uid, "Interested", "", "a" * 32, "Dana")
    again = calls.log_call(lead.uid, "Interested", "", "a" * 32, "Someone else")
    assert first["by"] == again["by"] == "Dana"
