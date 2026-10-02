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


# ---- the Calls page: every called business reachable, however many; its filter reads the calls

def _called(n):
    """n saved businesses, each called once (business i at time 1000 + i, so the last is the
    latest call), the outcomes taking turns; written in one go, as thousands of calls are."""
    leads = [_lead(f"Business {i}", f"n{i}", lat=40.3 + (i // 80) * 0.01, lon=-112.2 + (i % 80) * 0.01)
             for i in range(n)]
    saved.save_search(leads, config.DEFAULT_KEYWORDS)
    with store.connect() as db:
        db.many("INSERT INTO calls (id, uid, at, outcome, notes) VALUES (?, ?, ?, ?, ?)",
                [(f"{i:032x}", lead.uid, 1000.0 + i, calls.OUTCOMES[i % len(calls.OUTCOMES)], f"note {i}")
                 for i, lead in enumerate(leads)])
    return leads


def test_the_calls_page_reaches_every_called_business_past_five_thousand():
    """The Calls page gets one page at a time, latest call first, with "Show more" (offset)
    reaching the very first business ever called; its tab counts are every called business."""
    leads = _called(5100)
    client = web.create_app().test_client()
    body = client.get("/leads?tab=called&sort=called&dir=desc").get_json()
    assert len(body["leads"]) == web.leads.PAGE_SIZE and body["total"] == 5100
    assert body["leads"][0]["key"] == leads[-1].uid               # the latest call first
    per = 5100 // len(calls.OUTCOMES)
    assert body["call_counts"] == {"all": 5100, "outcomes": dict.fromkeys(calls.OUTCOMES, per)}
    last = client.get("/leads?tab=called&sort=called&dir=desc&offset=5000&limit=100").get_json()
    assert [l["key"] for l in last["leads"]][-1] == leads[0].uid  # the very first call, reachable
    assert len(last["leads"]) == 100 and last["total"] == 5100
    # One outcome's tab: only the businesses whose latest call went that way, every one reachable.
    tab = client.get("/leads?tab=called&sort=called&dir=desc&outcome=Bad+Lead&offset=800&limit=100").get_json()
    assert tab["total"] == per and {l["call_outcome"] for l in tab["leads"]} == {"Bad Lead"}
    assert tab["leads"][-1]["key"] == leads[5].uid                # the first Bad Lead call
    # An unknown outcome (or one asked of another tab) filters nothing.
    assert client.get("/leads?tab=called&outcome=Maybe").get_json()["total"] == 5100
    assert client.get("/leads?tab=all&outcome=Bad+Lead").get_json()["total"] == 5100


def test_the_calls_filter_finds_what_was_said_how_it_went_and_who_called():
    forklift, quote, other = _lead("Acme Freight", "a"), _lead("Bolt Supply", "b"), _lead("Cedar Market", "c")
    saved.save_search([forklift, quote, other])
    calls.log_call(forklift.uid, "Follow Up", "Uses a FORKLIFT to load the dumpster", by="Dana Smith")
    calls.log_call(forklift.uid, "Interested", "Wants a quote next week", by="Lee")
    calls.log_call(quote.uid, "No Contact", "", by="Dana Smith")
    calls.log_call(other.uid, "Not Interested", "Happy with their hauler", by="Lee")
    client = web.create_app().test_client()

    def names(query):
        body = client.get(f"/leads?tab=called&{query}").get_json()
        return sorted(l["name"] for l in body["leads"]), body["call_counts"]

    # A word from an earlier call's summary, in any case, finds the business.
    assert names("q=forklift")[0] == ["Acme Freight"]
    # A caller's name, a word of the summary and a town, together, in any order.
    assert names("q=dana")[0] == ["Acme Freight", "Bolt Supply"]
    assert names("q=dumpster+dana")[0] == ["Acme Freight"]
    # How a call went (any of them, not only the latest).
    assert names("q=no+contact")[0] == ["Bolt Supply"]
    # The tab counts follow the filter; the outcome's tab narrows further, by the latest call.
    rows, counts = names("q=lee")
    assert rows == ["Acme Freight", "Cedar Market"]
    assert counts["all"] == 2 and counts["outcomes"]["Interested"] == 1
    assert counts["outcomes"]["Not Interested"] == 1 and counts["outcomes"]["Follow Up"] == 0
    assert names("q=lee&outcome=Interested")[0] == ["Acme Freight"]
    assert names("q=lee&outcome=Follow+Up")[0] == []
    # The Leads page's filter is unchanged: it doesn't look in the calls.
    assert client.get("/leads?tab=all&q=forklift").get_json()["total"] == 0
    # A refresh (?since) of the Calls view sends the same counts and filter.
    since = client.get("/leads?tab=called&q=forklift").get_json()["now"] - 60
    changed = client.get(f"/leads?tab=called&q=forklift&since={since}").get_json()
    assert changed["call_counts"]["all"] == 1 and changed["total"] == 1
    assert {l["key"]: l["in_view"] for l in changed["leads"]}[forklift.uid] is True
    assert {l["key"]: l["in_view"] for l in changed["leads"]}[other.uid] is False
