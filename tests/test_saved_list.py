"""The saved list: a closed business nobody saved is never added, and refreshing
after a search stays small."""

import json
import time

import pytest

from leadgen import config, marks, saved, stats, web
from leadgen.models import Lead
from leadgen.scoring import score_lead


def _lead(name="Costco", sid="c", **kw):
    kw.setdefault("lat", 40.72)
    kw.setdefault("lon", -111.9)
    kw.setdefault("raw_categories", ["shop=wholesale"])
    lead = Lead(name=name, source="osm", source_id=sid, **kw)
    score_lead(lead, config.DEFAULT_KEYWORDS)
    return lead


# ---- a closed business nobody saved is never added

def test_a_new_closed_business_is_not_added_to_the_saved_list():
    open_one = _lead("Smith's", "s1", lat=40.75)
    closed = _lead("Old Mill Foods", "m1", business_status="CLOSED_PERMANENTLY")
    new, updated = saved.save_search([open_one, closed])
    assert (new, updated) == (1, 0)
    assert [l.name for l in saved.load()] == ["Smith's"]
    assert closed.uid == "" and open_one.uid
    # Found open later, it is added like any new business.
    saved.save_search([_lead("Old Mill Foods", "m1")])
    assert {l.name for l in saved.load()} == {"Smith's", "Old Mill Foods"}


def test_a_saved_business_that_closes_keeps_its_row():
    lead = _lead()
    saved.save_search([lead])
    saved.save_search([_lead(business_status="CLOSED_PERMANENTLY")])
    [row] = saved.load()
    assert row.uid == lead.uid and saved.CLOSED_FLAG in row.flags


# ---- refreshing after a search stays small, and the parsed list is reused

def _many(n):
    leads = [_lead(f"Biz {i}", f"n{i}", lat=40.5 + (i % 50) * 0.01, lon=-112 + (i // 50) * 0.01,
                   raw_categories=["shop=supermarket"]) for i in range(n)]
    saved.save_search(leads, config.DEFAULT_KEYWORDS)
    return leads


def test_a_refresh_after_a_big_search_sends_one_page_at_most(monkeypatch):
    monkeypatch.setattr(web.leads, "SINCE_OVERLAP", 0.0)
    leads = _many(1500)
    client = web.create_app().test_client()
    since = client.get("/leads?tab=unchecked&limit=300").get_json()["now"]
    time.sleep(0.01)
    saved.save_search(leads, config.DEFAULT_KEYWORDS)          # the search touches every lead
    res = client.get(f"/leads?since={since}&tab=unchecked&limit=300")
    body = res.get_json()
    assert body["reload"] and body["leads"] == [] and body["counts"]["unchecked"] == 1500
    assert len(res.data) < 20_000
    # A few changes come back as rows: in full when in the view, just the key otherwise.
    since = body["now"]
    time.sleep(0.01)
    marks.set_mark(leads[0].uid, "yes")
    body = client.get(f"/leads?since={since}&tab=unchecked&limit=300").get_json()
    assert body.get("reload") is None
    assert body["leads"] == [{"key": leads[0].uid, "in_view": False}]
    [row] = client.get(f"/leads?since={since}&tab=yes&limit=300").get_json()["leads"]
    assert row["key"] == leads[0].uid and row["in_view"] and row["has_baler"] == "yes"


def test_the_parsed_list_is_reused_until_a_search_changes_it(monkeypatch):
    _many(20)
    parsed = []
    real = saved._to_lead
    monkeypatch.setattr(saved, "_to_lead", lambda data: parsed.append(1) or real(data))
    first = saved.load()
    assert len(parsed) == 20
    again = saved.load()
    assert len(parsed) == 20 and [l.uid for l in again] == [l.uid for l in first]
    again[0].has_baler = "yes"                     # each caller gets its own copies
    assert saved.load()[0].has_baler == ""
    saved.save_search([_lead("Harmons", "h1", raw_categories=["shop=supermarket"])])
    parsed.clear()
    assert len(saved.load()) == 21 and len(parsed) == 21     # parsed again after the change


# ---- the filter box: every word typed, in any order

def test_several_words_find_a_lead_when_each_is_somewhere_in_its_row():
    saved.save_search([
        _lead("Walmart Supercenter", "w1", city="Layton", lat=41.06),
        _lead("Walmart Supercenter", "w2", city="Ogden", lat=41.22),
        _lead("Smith's Marketplace", "s1", city="Layton", lat=41.07, lon=-111.95,
              raw_categories=["shop=supermarket"])])
    client = web.create_app().test_client()

    def names(q):
        rows = client.get(f"/leads?tab=all&q={q}").get_json()["leads"]
        return sorted(f"{r['name']} ({r['city']})" for r in rows)
    assert names("walmart") == ["Walmart Supercenter (Layton)", "Walmart Supercenter (Ogden)"]
    assert names("walmart layton") == names("Layton  WALMART") == ["Walmart Supercenter (Layton)"]
    assert names("layton") == ["Smith's Marketplace (Layton)", "Walmart Supercenter (Layton)"]
    assert names("walmart provo") == []


# ---- the company's name, as config.OWN_COMPANY has it now (AARCO Waste & Disposal)

@pytest.mark.parametrize("old_flag", ["OWN COMPANY: Arco Compactor", "OWN COMPANY: AARCO Compactor",
                                      "OWN COMPANY: AARCO Compactors & Balers"])
def test_aarcos_own_listing_saved_under_an_old_name_shows_the_name_now(old_flag):
    """A row saved under an earlier name of the company (the "Arco" spelling, the short
    "AARCO Compactor", then "AARCO Compactors & Balers") is shown with config.OWN_COMPANY;
    the row keeps its text."""
    from leadgen import store
    lead = _lead("AARCO Compactor", "a1")
    saved.save_search([lead])
    with store.connect() as db:
        [(text,)] = db.all("SELECT lead FROM leads WHERE uid = ?", (lead.uid,))
        old = text.replace(f"OWN COMPANY: {config.OWN_COMPANY}", old_flag)
        assert old != text
        db.run("UPDATE leads SET lead = ?, last_seen = last_seen + 1 WHERE uid = ?", (old, lead.uid))
    [row] = saved.load()
    assert row.flags == [f"OWN COMPANY: {config.OWN_COMPANY}"] == ["OWN COMPANY: AARCO Waste & Disposal"]
    assert row.lead_type == "Own company"
    with store.connect() as db:
        assert db.one("SELECT lead FROM leads WHERE uid = ?", (lead.uid,))[0] == old


def test_a_listing_flagged_by_a_name_inside_another_word_shows_as_scored_now():
    """A row flagged AARCO's own or a competitor's when the names matched inside other
    words (scoring rules version 1: "Marco Compactor Repair", "Repro Balers") is shown
    with the type its category gives, as a search now scores it; AARCO's own and a real
    competitor keep their flags, and no saved row is rewritten."""
    from leadgen import store
    from leadgen.scoring import INDUSTRY_FLAG, OWN_FLAG
    leads = [_lead("Marco Compactor Repair", "m", raw_categories=[]), _lead("Repro Balers", "r", raw_categories=[]),
             _lead("Valley Storage Co", "v", raw_categories=[], lat=40.75),
             _lead("AARCO Waste & Disposal", "a", raw_categories=[], lat=40.7),
             _lead("Pro Baler", "p", raw_categories=[], lat=40.68)]
    saved.save_search(leads)
    stale = {"Marco Compactor Repair": ("Own company", [OWN_FLAG]),
             "Repro Balers": ("Competitor", ["Temporarily closed", "COMPETITOR: Pro Baler"]),
             "Valley Storage Co": ("Own company", ["OWN COMPANY: Arco Compactor"])}
    olds = {}
    with store.connect() as db:
        for lead in leads:
            if lead.name in stale:
                data = json.loads(db.one("SELECT lead FROM leads WHERE uid = ?", (lead.uid,))[0])
                data["lead_type"], data["flags"] = stale[lead.name]          # as version 1 saved it
                olds[lead.uid] = json.dumps(data)
                db.run("UPDATE leads SET lead = ?, last_seen = last_seen + 1 WHERE uid = ?",
                       (olds[lead.uid], lead.uid))
    shown = {l.name: (l.lead_type, l.flags) for l in saved.load()}
    assert shown["Marco Compactor Repair"] == ("Industry (equipment / hauler)", [INDUSTRY_FLAG])
    assert shown["Repro Balers"] == ("Industry (equipment / hauler)", ["Temporarily closed", INDUSTRY_FLAG])
    assert shown["Valley Storage Co"][1] == [] and shown["Valley Storage Co"][0] not in ("Own company", "Competitor")
    assert shown["AARCO Waste & Disposal"] == ("Own company", [OWN_FLAG])
    assert shown["Pro Baler"] == ("Competitor", ["COMPETITOR: Pro Baler"])
    with store.connect() as db:
        for uid, old in olds.items():
            assert db.one("SELECT lead FROM leads WHERE uid = ?", (uid,))[0] == old


def test_a_parent_company_listing_saved_before_it_was_recognised_shows_as_aarcos_own():
    """A listing of AARCO's parent company saved as a prospect (before config.PARENT_*)
    is shown flagged as the parent's, not a prospect: out of "Not checked" and Stats.
    The saved row is not rewritten."""
    from leadgen import store
    from leadgen.scoring import PARENT_FLAG
    ace = _lead("Ace Intermountain Recycling Center", "ace", raw_categories=["amenity=recycling"],
                address="1240 South Wallace Road", city="Salt Lake City")
    other = _lead("Ace Hardware", "hw", raw_categories=["shop=hardware"])
    saved.save_search([ace, other])
    with store.connect() as db:
        [(text,)] = db.all("SELECT lead FROM leads WHERE uid = ?", (ace.uid,))
        data = json.loads(text)
        assert data["flags"] == [PARENT_FLAG]
        data.update(flags=[], lead_type="Waste / recycling facility")      # as saved before
        old = json.dumps(data)
        db.run("UPDATE leads SET lead = ?, last_seen = last_seen + 1 WHERE uid = ?", (old, ace.uid))
    rows = {l.name: l for l in saved.load()}
    shown = rows["Ace Intermountain Recycling Center"]
    assert shown.lead_type == "Own company" and shown.flags == [PARENT_FLAG]
    assert PARENT_FLAG == "OWN COMPANY: AARCO's parent company (ACE Recycling & Disposal)"
    assert rows["Ace Hardware"].lead_type == "Prospect" and rows["Ace Hardware"].flags == []
    with store.connect() as db:
        assert db.one("SELECT lead FROM leads WHERE uid = ?", (ace.uid,))[0] == old
    client = web.create_app(password="").test_client()
    not_checked = [l["name"] for l in client.get("/leads?tab=unchecked").get_json()["leads"]]
    assert not_checked == ["Ace Hardware"]
    flagged = client.get("/leads?tab=competitors").get_json()["leads"]
    assert [(l["name"], l["prospect"], l["flags"]) for l in flagged] == [
        ("Ace Intermountain Recycling Center", False, [PARENT_FLAG])]
    summary = stats.summarize(marks.apply(saved.load()))
    assert summary["left_out"] == 1 and sum(t["saved"] for t in summary["by_tier"]) == 1
