"""The Leads and Calls pages' list is read once and kept (web/leads.py Listing): every
answer is the one reading the whole list each time would give, and after a click only
the leads it changed are read again."""

import itertools
import time
import uuid

import pytest

from leadgen import calls, config, contacts, marks, saved, store, web
from leadgen.models import Lead
from leadgen.scoring import score_lead

leads_page = web.leads

KINDS = [["shop=supermarket"], ["building=warehouse"], ["amenity=hospital"], ["tourism=hotel"],
         ["shop=mall"], ["yelp:junkremoval"]]
TOWNS = ["Salt Lake City", "Layton", "", "Murray", "Sandy"]


def _lead(i, **kw):
    kinds = KINDS[i % len(KINDS)]
    word = ["Foods", "Storage", "Care", "Inn", "Mart"][i % 5]
    name = f"Pro Baler {i}" if kinds == ["yelp:junkremoval"] else f"{word} {i}"       # a competitor, or not
    kw.setdefault("footprint_sqft", [None, 20000, 60000, 150000][i % 4])             # tiers B to D
    lead = Lead(name=name, lat=40.5 + (i % 13) * 0.03, lon=-112.1 + (i % 7) * 0.04, source="osm",
                source_id=f"n{i}", raw_categories=kinds, city=TOWNS[i % len(TOWNS)],
                phone="(801) 555-0100" if i % 3 else "", address=f"{100 + i} Main St" if i % 4 else "", **kw)
    score_lead(lead, config.DEFAULT_KEYWORDS)
    return lead


@pytest.fixture
def seeded():
    """90 saved businesses of every kind: marked Yes and No, called (each result), with
    verified contacts, and a few closed for good."""
    leads = [_lead(i) for i in range(90)]
    saved.save_search(leads, config.DEFAULT_KEYWORDS)
    saved.save_search([_lead(i, business_status="CLOSED_PERMANENTLY") for i in range(0, 90, 11)],
                      config.DEFAULT_KEYWORDS)
    for i, lead in enumerate(leads):
        if i % 4 == 1:
            marks.set_mark(lead.uid, "yes" if i % 8 == 1 else "no", "Dana")
        if i % 5 == 2:
            calls.log_call(lead.uid, calls.OUTCOMES[i % len(calls.OUTCOMES)], f"Spoke to Dana about bale {i}",
                           by="Sam")
        if i % 9 == 3:
            contacts.save(lead.uid, "(801) 555-0199" if i % 2 else "", "Jane Doe, facilities", "Sam")
    return leads


# ---- the answers are those of reading everything each time (the code before the list was kept)

def _in_view(lead, view):
    if view == "all":
        return True
    if view == "competitors":
        return not leads_page.is_prospect(lead)
    if view == "called":
        return bool(lead.call_count)
    if view == "closed":
        return saved.is_closed(lead)
    if view == "unchecked" and saved.is_closed(lead):
        return False
    return leads_page.is_prospect(lead) and (lead.has_baler or "unchecked") == view


def _expected(view, q="", tier="", phone=False, sort="score", desc=None, outcome="", keep=(), offset=0,
              limit=100):
    leads, _ = web.load_saved()
    text = calls.search_text() if view == "called" and q else {}
    rows = [l for l in leads if (_in_view(l, view) or (l.uid in keep and not offset))
            and (not outcome or l.call_outcome == outcome)
            and leads_page.matches(l, q, tier, phone, text.get(l.uid, ""))]
    desc = sort in ("score", "called") if desc is None else desc
    if sort != "score" or not desc:
        rows.sort(key=leads_page.SORTS[sort], reverse=desc)
    elif view == "unchecked":
        rows.sort(key=lambda l: (-l.score, not leads_page.has_phone(l)))
    counts = dict.fromkeys(leads_page.VIEWS, 0) | {"all": len(leads)}
    outcomes = dict.fromkeys(calls.OUTCOMES, 0)
    for lead in leads:
        for v in leads_page.VIEWS:
            if v != "all" and _in_view(lead, v):
                counts[v] += 1
        if lead.call_count and lead.call_outcome in outcomes:
            outcomes[lead.call_outcome] += 1
    return [l.uid for l in rows[offset:offset + limit]], len(rows), {**counts, "outcomes": outcomes}


def _asked(client, view, q="", tier="", phone=False, sort="score", desc=None, outcome="", keep=(), offset=0,
           limit=100):
    args = {"tab": view, "q": q, "tier": tier, "phone": "1" if phone else "", "sort": sort,
            "dir": "" if desc is None else ("desc" if desc else "asc"), "outcome": outcome,
            "keep": ",".join(keep), "offset": offset, "limit": limit}
    body = client.get("/leads", query_string=args).get_json()
    return [l["key"] for l in body["leads"]], body["total"], body["counts"]


def test_every_tab_sort_and_filter_gives_what_reading_the_whole_list_gives(seeded):
    client = web.create_app().test_client()
    for view, sort, desc in itertools.product(leads_page.VIEWS, leads_page.SORTS, (None, True, False)):
        assert _asked(client, view, sort=sort, desc=desc) == _expected(view, sort=sort, desc=desc), (view, sort, desc)
    for view, q, tier, phone in itertools.product(("unchecked", "all", "called", "yes"),
                                                  ("", "foods", "layton 1", "jane", "dana bale"),
                                                  ("", "A", "B", "D"), (False, True)):
        assert _asked(client, view, q, tier, phone) == _expected(view, q, tier, phone), (view, q, tier, phone)
    for outcome in calls.OUTCOMES:
        assert _asked(client, "called", outcome=outcome) == _expected("called", outcome=outcome)
    assert _asked(client, "all", offset=40, limit=25) == _expected("all", offset=40, limit=25)
    # Rows just marked stay put (keep) in their place, on the first page only.
    yes = [l.uid for l in seeded if l.uid and marks.get_all([l.uid]).get(l.uid) == "yes"][:2]
    for sort in leads_page.SORTS:
        assert _asked(client, "unchecked", sort=sort, keep=yes) == _expected("unchecked", sort=sort, keep=yes)
    assert _asked(client, "unchecked", keep=yes, offset=10) == _expected("unchecked", keep=yes, offset=10)


# ---- kept between requests; after a click only what it changed is read again

def _same(found, fresh):
    """found (kept and changed in place) is the list read in full (fresh)."""
    uids = lambda rows: [l.uid for l in rows]                    # noqa: E731
    assert uids(found.leads) == uids(fresh.leads)
    assert {v: uids(r) for v, r in found.views.items()} == {v: uids(r) for v, r in fresh.views.items()}
    assert found.counts == fresh.counts and found.sums == fresh.sums and found.tabs == fresh.tabs
    for (view, sort, desc), rows in found._ordered.items():
        assert uids(rows) == uids(fresh.ordered(view, sort, desc)), (view, sort, desc)
    if found._text is not None:
        assert found._text == fresh.text()
    for lead in found.leads:
        assert web.lead_json(lead) == web.lead_json(fresh.by_uid[lead.uid])


def test_a_click_reads_again_only_the_business_it_changed(seeded, monkeypatch):
    reads = []
    real = leads_page.load_saved
    monkeypatch.setattr(leads_page, "load_saved", lambda *a: reads.append(1) or real(*a))
    client = web.create_app().test_client()
    for view, sort, desc in itertools.product(leads_page.VIEWS, leads_page.SORTS, (True, False)):
        client.get("/leads", query_string={"tab": view, "sort": sort, "dir": "desc" if desc else "asc"})
    client.get("/leads?tab=unchecked&q=foods")
    assert reads == [1]                                  # read once, then kept
    lead, other, competitor = seeded[4], seeded[8], seeded[5]
    clicks = [lambda: marks.set_mark(lead.uid, "yes", "Dana"),
              lambda: marks.set_mark(lead.uid, "no", "Dana"),
              lambda: marks.undo(marks.pending_undos()[lead.uid]["id"]),
              lambda: calls.log_call(other.uid, "Interested", "wants a quote", by="Sam"),
              lambda: calls.undo(calls.pending_undos()[other.uid]["id"]),
              lambda: calls.log_call(competitor.uid, "Bad Lead", "", by="Sam"),
              lambda: contacts.save(other.uid, "(801) 555-0142", "Lee", "Sam"),
              lambda: contacts.save(other.uid, "", "", "Sam")]
    for click in clicks:
        assert click() is not False
        body = client.get("/leads?tab=unchecked").get_json()
        assert body["counts"] == _expected("unchecked")[2]
        found = leads_page.listing()
        _same(found, leads_page.Listing.read(found.key, 0.0, real()[0]))
    assert reads == [1]                                  # never read in full again


RESTORED = {  # rows a restore from a backup adds, with their old times
    "calls": ("INSERT INTO calls (id, uid, at, outcome, notes) VALUES (?, ?, ?, ?, ?)",
              lambda uid, at: (uuid.uuid4().hex, uid, at, "Interested", "restored")),
    "lead_contacts": ("INSERT INTO lead_contacts (id, uid, phone, contact, by_name, at) VALUES (?, ?, ?, ?, ?, ?)",
                      lambda uid, at: (uuid.uuid4().hex, uid, "(801) 555-0177", "Restored Rae", "Sam", at)),
    "marks": ("INSERT INTO marks (uid, value, updated_at) VALUES (?, ?, ?)", lambda uid, at: (uid, "yes", at)),
}


@pytest.mark.parametrize("table", RESTORED)
def test_rows_that_come_in_without_a_click_are_read_in_full(seeded, monkeypatch, table):
    """A restore from a backup adds calls, contacts or marks with their old times: the
    list is read in full, so nothing is missed."""
    reads = []
    real = leads_page.load_saved
    monkeypatch.setattr(leads_page, "load_saved", lambda *a: reads.append(1) or real(*a))
    client = web.create_app().test_client()
    client.get("/leads?tab=all")
    lead = seeded[6]                                    # not marked, called or verified
    sql, row = RESTORED[table]
    with store.connect() as db:
        db.run(sql, row(lead.uid, time.time() - 86400))
    for view in ("all", "called", "yes"):
        assert _asked(client, view) == _expected(view)
    found = leads_page.listing()
    _same(found, leads_page.Listing.read(found.key, 0.0, real()[0]))
    assert reads == [1, 1]
    # A search changes the saved rows: read in full as well.
    saved.save_search([_lead(500)], config.DEFAULT_KEYWORDS)
    assert _asked(client, "all") == _expected("all") and len(reads) == 3
