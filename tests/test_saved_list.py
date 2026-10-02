"""The saved list: a closed business nobody saved is never added, and refreshing
after a search stays small."""

import time

from leadgen import config, marks, saved, web
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
