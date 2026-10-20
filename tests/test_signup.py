"""Sign-up mode (LEADGEN_SIGNUP=1, signup_mode.py): signing up, logging in by email, the
emailed links, the company's settings and team, the search allowance, and above all that
each company sees only its own data, through the web routes, on SQLite and (with
LEADGEN_TEST_DATABASE_URL) on Postgres. Without the flag none of it is there (the rest of
the tests are AARCO's own site, unchanged)."""

import os
import re
import threading
import time
from contextlib import contextmanager

import pytest

from leadgen import accounts, allowance, calls, company, config, customers, daily, saved, store, web
from leadgen.models import Lead
from leadgen.pipeline import RunResult
from leadgen.scoring import score_lead

PASSWORD = "correct horse battery"


@pytest.fixture
def signup(monkeypatch):
    """Sign-up mode on, with emailed links caught (to, subject, link) instead of sent."""
    monkeypatch.setenv("LEADGEN_SIGNUP", "1")
    monkeypatch.setenv("SECRET_KEY", "tests-only-secret")
    sent: list[tuple[str, str, str]] = []

    def catch(to, subject, body, link):
        sent.append((to, subject, link))
        return True
    monkeypatch.setattr(web.accounts, "send_email", catch)
    return sent


@pytest.fixture
def app(signup):
    return web.create_app()


def _sign_up(client, company_name="Acme Balers", email="owner@acme.example", name="Pat Owner",
             set_up=True, competitors="Rival Waste"):
    res = client.post("/signup", data={"company": company_name, "name": name, "email": email,
                                       "password": PASSWORD})
    assert res.status_code == 302 and res.headers["Location"].endswith("/setup"), res.get_data(as_text=True)
    if set_up:
        res = client.post("/setup", data={"address": "84101", "radius": "30", "search_words": "pallets",
                                          "competitors": competitors})
        assert res.status_code == 302 and res.headers["Location"].endswith("/#find"), res.get_data(as_text=True)
    return accounts.user_by_email(email).company_id


@contextmanager
def _in(company_id):
    """Inside, the code works on this company's data and settings (as its requests do)."""
    token = company.use(accounts.get_company(company_id))
    try:
        with store.for_company(company_id):
            yield
    finally:
        company.reset(token)


def _lead(name="Costco", sid="c", **kw):
    kw.setdefault("lat", 40.76)
    kw.setdefault("lon", -111.89)
    kw.setdefault("raw_categories", ["shop=wholesale"])
    lead = Lead(name=name, source="osm", source_id=sid, phone="801-555-0101", address="1 Main St",
                city="Salt Lake City", zip="84101", **kw)
    score_lead(lead, config.DEFAULT_KEYWORDS)
    return lead


def _wait(client, job):
    for _ in range(400):
        body = client.get(f"/status/{job}").get_json()
        if body["state"] != "running":
            return body
        time.sleep(0.05)
    raise AssertionError("the search never finished")


def _search(client, monkeypatch, leads):
    monkeypatch.setattr(web.finding, "run", lambda params, progress: RunResult(
        list(leads), (40.7559, -111.8967), "Salt Lake City", [], {"leads kept": len(leads)}))
    res = client.post("/search", data={"location": "84101", "radius": "30"})
    assert res.status_code == 200, res.get_json()
    job = res.get_json()["job_id"]
    assert _wait(client, job)["state"] == "done"
    return job


def _page(client, path="/"):
    return client.get(path, headers={"Accept": "text/html"})


# ---- off by default: AARCO's site as it was

def test_without_the_flag_there_is_no_sign_up(monkeypatch):
    client = web.create_app().test_client()
    assert client.get("/signup").status_code == 404
    assert client.post("/settings", data={}).status_code == 404
    assert client.get("/setup").status_code == 404
    page = _page(client).get_data(as_text=True)
    assert "AARCO Waste &amp; Disposal" in page and 'id="page-settings"' not in page


def test_the_company_accessor_reads_aarcos_settings_without_the_flag():
    now = company.current()
    assert now.name == config.OWN_COMPANY and now.site_name == config.SITE_NAME
    assert now.center == config.SERVICE_CENTER and now.competitors == config.COMPETITORS
    assert store.company_id() is None


# ---- signing up and logging in

def test_sign_up_logs_in_and_the_setup_puts_the_company_on_its_pages(app):
    client = app.test_client()
    assert _page(client).headers["Location"].endswith("/welcome")       # logged out: the landing page
    assert client.get("/leads").status_code == 401
    cid = _sign_up(client)
    found = accounts.get_company(cid)
    assert found.status == "trialing" and found.plan == "trial" and found.located
    assert found.trial_ends_at > time.time() + 13 * 86400
    assert found.search_words == ("pallets",) and found.competitors == ("Rival Waste",)
    page = _page(client).get_data(as_text=True)
    shown = re.sub(r"<style>.*?</style>|<!--.*?-->", "", page, flags=re.S)    # what the page says
    assert "<title>Lead Finder</title>" in page and "Acme Balers" in page and "AARCO" not in shown
    assert 'id="page-settings"' in page and "Verify your email" in page
    assert "4 of 4 searches left this month" in page
    assert client.get("/leads").status_code == 200


def test_signing_up_refuses_short_passwords_and_a_taken_email(app):
    client = app.test_client()
    res = client.post("/signup", data={"company": "A", "name": "B", "email": "a@b.example", "password": "short"})
    assert res.status_code == 400 and "at least 10 characters" in res.get_data(as_text=True)
    _sign_up(client, email="a@b.example", set_up=False)
    other = app.test_client()
    res = other.post("/signup", data={"company": "C", "name": "D", "email": "A@B.example ", "password": PASSWORD})
    assert res.status_code == 400 and "already an account" in res.get_data(as_text=True)


def test_login_by_email_and_password_with_a_limit_on_wrong_tries(app):
    _sign_up(app.test_client(), set_up=False)
    client = app.test_client()
    res = client.post("/login", data={"email": "owner@acme.example", "password": "wrong password!"})
    assert res.status_code == 401 and "Wrong email or password" in res.get_data(as_text=True)
    res = client.post("/login", data={"email": "OWNER@acme.example", "password": PASSWORD})
    assert res.status_code == 302 and client.get("/leads").status_code == 200
    client.post("/logout")
    assert client.get("/leads").status_code == 401
    for _ in range(10):
        client.post("/login", data={"email": "owner@acme.example", "password": "wrong password!"})
    res = client.post("/login", data={"email": "owner@acme.example", "password": PASSWORD})
    assert res.status_code == 429


def test_too_many_sign_ups_from_one_address(app):
    client = app.test_client()
    for n in range(5):
        _sign_up(client, email=f"p{n}@x.example", set_up=False)
        client.post("/logout")
    res = client.post("/signup", data={"company": "X", "name": "Y", "email": "p9@x.example", "password": PASSWORD})
    assert res.status_code == 429


def test_a_password_change_ends_the_other_logins(app, signup):
    client = app.test_client()
    _sign_up(client, set_up=False)
    other = app.test_client()
    assert other.post("/forgot", data={"email": "owner@acme.example"}).status_code == 200
    to, _, link = signup[-1]
    assert to == "owner@acme.example" and "/reset/" in link
    path = link.split("localhost", 1)[1]
    assert other.get(path).status_code == 200
    res = other.post(path, data={"password": "a brand new password"})
    assert res.status_code == 302 and other.get("/leads").status_code == 200
    assert client.get("/leads").status_code == 401                    # the old login ended
    assert other.get(path).status_code == 404                          # used once only
    assert accounts.check_login("owner@acme.example", "a brand new password")


def test_forgot_says_the_same_for_an_unknown_email(app, signup):
    res = app.test_client().post("/forgot", data={"email": "nobody@nowhere.example"})
    assert res.status_code == 200 and "If there is an account" in res.get_data(as_text=True)
    assert signup == []


def test_the_email_link_confirms_the_address(app, signup):
    client = app.test_client()
    _sign_up(client)
    to, _, link = next(s for s in signup if "/verify/" in s[2])
    assert to == "owner@acme.example"
    assert client.get(link.split("localhost", 1)[1]).status_code == 200
    assert accounts.user_by_email(to).verified
    assert "Verify your email" not in _page(client).get_data(as_text=True)


def test_without_email_set_up_the_link_goes_to_the_log(monkeypatch, caplog):
    monkeypatch.setenv("LEADGEN_SIGNUP", "1")
    client = web.create_app().test_client()
    with caplog.at_level("WARNING", logger="leadgen.web"):
        _sign_up(client, set_up=False)
    assert re.search(r"wasn't emailed to owner@acme\.example: http://localhost/verify/\S+", caplog.text)
    assert client.get("/leads").status_code == 200                    # let in all the same


# ---- settings and the team

def test_the_owner_changes_the_settings_and_invites_a_teammate(app, signup):
    owner = app.test_client()
    cid = _sign_up(owner)
    res = owner.post("/settings", data={"name": "Acme Compactor Co", "address": "84101", "radius": "20",
                                        "search_words": "cardboard, pallets", "competitors": "Rival Waste, Other Co"})
    assert res.status_code == 302 and res.headers["Location"].endswith("/#settings")
    found = accounts.get_company(cid)
    assert found.name == "Acme Compactor Co" and found.radius_miles == 20
    assert found.competitors == ("Rival Waste", "Other Co")
    assert "Your settings are saved." in _page(owner).get_data(as_text=True)
    owner.post("/settings/invite", data={"email": "mate@acme.example"})
    to, _, link = signup[-1]
    assert to == "mate@acme.example" and "/invite/" in link
    mate = app.test_client()
    path = link.split("localhost", 1)[1]
    assert "mate@acme.example" in mate.get(path).get_data(as_text=True)
    assert mate.post(path, data={"name": "Sam Mate", "password": PASSWORD}).status_code == 302
    user = accounts.user_by_email("mate@acme.example")
    assert user.company_id == cid and user.role == "member" and user.verified
    # A member can't change the settings.
    assert mate.post("/settings", data={"name": "Taken over", "address": "84101", "radius": "5"}).status_code == 403
    assert "Sam Mate" in _page(owner).get_data(as_text=True)


def test_competitors_and_the_own_listing_are_the_companys(app):
    cid = _sign_up(app.test_client(), competitors="Rival Waste")
    with _in(cid):
        rival, own, aarco = _lead("Rival Waste Services", "r"), _lead("Acme Balers", "a"), _lead("Pro Baler", "p")
    assert rival.lead_type == "Competitor" and rival.flags[0] == "COMPETITOR: Rival Waste"
    assert own.lead_type == "Own company" and "OWN COMPANY: Acme Balers" in own.flags
    assert aarco.lead_type != "Competitor"                  # AARCO's competitors aren't this company's
    assert _lead("Pro Baler", "p2").lead_type != "Competitor"   # nor anyone's outside a company


# ---- each company's own data

def test_a_search_started_by_a_company_saves_into_its_data_only(app, monkeypatch):
    a, b = app.test_client(), app.test_client()
    first = _sign_up(a, "Acme Balers", "owner@acme.example")
    second = _sign_up(b, "Beta Waste", "owner@beta.example")
    seen: list[str | None] = []
    real = saved.save_search

    def save(*args, **kw):
        seen.append((store.company_id(), threading.current_thread().name))
        return real(*args, **kw)
    monkeypatch.setattr(saved, "save_search", save)
    job = _search(a, monkeypatch, [_lead(), _lead("Walmart", "w")])
    assert [c for c, _ in seen] == [first] and seen[0][1].startswith("leadgen search")
    with _in(first):
        assert sorted(l.name for l in saved.load()) == ["Costco", "Walmart"]
    with _in(second):
        assert saved.load() == []
    assert saved.load() == []                                 # nor AARCO's (the base database)
    # The other company can't follow the search or download it.
    assert b.get(f"/status/{job}").status_code == 404
    assert b.get(f"/download/{job}.csv").status_code == 404
    assert a.get(f"/download/{job}.csv").status_code == 200


def test_two_companies_see_only_their_own_data_through_every_page(app, monkeypatch):
    a, b = app.test_client(), app.test_client()
    _sign_up(a, "Acme Balers", "owner@acme.example")
    _sign_up(b, "Beta Waste", "owner@beta.example")
    job = _search(a, monkeypatch, [_lead()])
    uid = a.get("/leads").get_json()["leads"][0]["key"]
    by = {"by": "Pat"}
    assert a.post("/mark", json={"key": uid, "value": "yes", **by}).status_code == 200
    assert a.post("/calls", json={"key": uid, "outcome": calls.OUTCOMES[0], "notes": "Spoke to Kim", **by}
                  ).status_code == 200
    assert a.post("/contact", json={"key": uid, "phone": "801-555-0199", "contact": "Kim", **by}).status_code == 200
    assert b.post("/mark", json={"key": uid, "value": "no", **by}).status_code == 404   # not in B's list
    # A sees all of it ...
    lead = a.get("/leads?tab=yes").get_json()
    assert lead["total"] == 1 and lead["leads"][0]["call_notes"] == "Spoke to Kim"
    assert a.get("/stats").get_json()["saved"] == 1
    assert len(a.get("/map-data").get_json()["pins"]) == 1
    assert "Costco" in a.get("/download/saved.csv").get_data(as_text=True)
    assert a.get("/searches").get_json()["used_today"]
    # ... and B none of it.
    assert b.get("/leads").get_json()["total"] == 0
    assert b.get("/leads?tab=yes").get_json()["total"] == 0
    assert b.get(f"/calls/{uid}").get_json().get("calls", []) == []
    assert b.get("/stats").get_json()["saved"] == 0
    assert b.get("/map-data").get_json()["pins"] == []
    assert "Costco" not in b.get("/download/saved.csv").get_data(as_text=True)
    history = b.get("/searches").get_json()
    assert not history["used_today"] and history["searches"] == []
    assert b.get(f"/status/{job}").status_code == 404
    # B's own search, the same day, is its own (A's day doesn't count for B).
    _search(b, monkeypatch, [_lead("Target", "t")])
    assert [l["name"] for l in b.get("/leads").get_json()["leads"]] == ["Target"]
    assert [l["name"] for l in a.get("/leads").get_json()["leads"]] == ["Costco"]


def test_each_company_has_its_own_customer_salt_and_fingerprints(app):
    a, b = app.test_client(), app.test_client()
    first = _sign_up(a, "Acme Balers", "owner@acme.example")
    second = _sign_up(b, "Beta Waste", "owner@beta.example")
    salt_a, salt_b = a.get("/customers").get_json()["salt"], b.get("/customers").get_json()["salt"]
    assert salt_a and salt_b and salt_a != salt_b
    hashes = sorted(customers.fingerprint(salt_a, k) for k in customers.keys(
        "Costco", "801-555-0101", "1 Main St", "Salt Lake City", "84101"))
    assert a.post("/customers", json={"hashes": hashes, "customers": 1, "by": "Pat"}).status_code == 200
    assert a.get("/customers").get_json()["keys"] == len(hashes)
    assert b.get("/customers").get_json()["keys"] == 0
    with _in(first):
        assert customers.on_file()[1] == set(hashes)
    with _in(second):
        assert customers.on_file()[1] == set()


# ---- the search allowance

def test_the_monthly_allowance_and_the_plan_can_stop_a_search(app, monkeypatch):
    client = app.test_client()
    cid = _sign_up(client)
    found = accounts.get_company(cid)
    with _in(cid):
        assert allowance.allowance(found).left == 4 and allowance.can_search(found) is None
        month = daily.today()[:8]
        with store.connect() as db:
            for day in ("01", "02", "03", "04"):
                if month + day != daily.today():
                    db.run("INSERT INTO searches (day, at, info) VALUES (?, ?, ?)",
                           (month + day, time.time(), '{"leads": 1}'))
    with _in(cid):
        used = allowance.used_this_month()
    res = client.post("/search", data={"location": "84101"})
    if used >= 4:
        assert res.status_code == 403 and "searches are used up" in res.get_json()["error"]
    for status, words in (("past_due", "payment didn't go through"), ("canceled", "plan is canceled")):
        accounts.update_company(cid, status=status)
        res = client.post("/search", data={"location": "84101"})
        assert res.status_code == 403 and words in res.get_json()["error"]
    accounts.update_company(cid, status="trialing", trial_ends_at=time.time() - 1)
    res = client.post("/search", data={"location": "84101"})
    assert res.status_code == 403 and "free trial has ended" in res.get_json()["error"]


def test_a_company_must_set_up_its_shop_before_searching(app):
    client = app.test_client()
    _sign_up(client, set_up=False)
    res = client.post("/search", data={"location": "84101"})
    assert res.status_code == 403 and "shop's address" in res.get_json()["error"]


def test_the_allowance_counts_this_months_searches_only(app):
    cid = _sign_up(app.test_client())
    with _in(cid):
        with store.connect() as db:
            db.run("INSERT INTO searches (day, at, info) VALUES (?, ?, ?)", ("1999-01-01", 1.0, "{}"))
        assert allowance.used_this_month() == 0
        found = accounts.get_company(cid)
        text = allowance.allowance(found).text()
    assert text.startswith("4 of 4 searches left this month; the count starts again on ")


def test_the_sign_up_pages_are_well_formed_html(app, signup):
    from test_html_valid import html_problems
    client = app.test_client()
    pages = {path: _page(client, path) for path in ("/signup", "/login", "/forgot")}
    _sign_up(client, set_up=False)
    pages["/setup"] = _page(client, "/setup")
    client.post("/settings/invite", data={"email": "mate@acme.example"})
    pages["/"] = _page(client)
    joining = signup[-1][2].split("localhost", 1)[1]
    pages["invite"] = _page(app.test_client(), joining)
    pages["expired"] = _page(app.test_client(), "/reset/not-a-token")
    for name, res in pages.items():
        page = res.get_data(as_text=True)
        assert page.lstrip().lower().startswith("<!doctype html>"), name
        assert html_problems(page) == [], name


# ---- the storage underneath

def test_each_company_has_its_own_database(signup):
    with store.for_company("abc123def4"):
        key = store.database_key()
        saved.save_search([_lead()])
        assert len(saved.load()) == 1
    assert key != store.database_key() and saved.load() == []
    with store.for_company("zzz999yyy8"):
        assert saved.load() == []
    with pytest.raises(ValueError):
        store.set_company("../etc")
    if store.database_url():
        assert key.endswith("c_abc123def4")
    else:
        assert key.endswith("companies/abc123def4.db")


def test_the_accounts_are_kept_apart_from_every_company(signup):
    first, _ = accounts.sign_up("Acme", "Pat", "pat@acme.example", PASSWORD)
    with store.for_company(first.id):
        assert accounts.user_by_email("pat@acme.example") is not None    # read from the base
        with store.connect() as db:
            tables = ("SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
                      if db.postgres else "SELECT name FROM sqlite_master WHERE type = 'table'")
            names = {r[0] for r in db.all(tables)}
    assert "users" not in names and "leads" in names


# ---- in a browser

def test_signing_up_and_the_settings_page_in_a_browser(signup, monkeypatch):
    from test_browser import _context
    from werkzeug.serving import make_server
    sync_api = pytest.importorskip("playwright.sync_api")
    server = make_server("127.0.0.1", 0, web.create_app(), threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    site = f"http://127.0.0.1:{server.server_port}/"
    try:
        with sync_api.sync_playwright() as p:
            try:
                chromium = p.chromium.launch(executable_path=os.environ.get("LEADGEN_CHROMIUM") or None)
            except Exception as exc:
                pytest.skip(f"Chromium is not available: {exc}")
            tab = _context(chromium).new_page()
            errors = []
            tab.on("pageerror", lambda exc: errors.append(str(exc)))
            tab.goto(site + "signup")
            tab.fill("input[name=company]", "Acme Balers")
            tab.fill("input[name=name]", "Pat Owner")
            tab.fill("input[name=email]", "owner@acme.example")
            tab.fill("input[name=password]", PASSWORD)
            tab.click("button[type=submit]")
            tab.wait_for_url("**/setup")
            tab.fill("input[name=address]", "84101")
            tab.fill("textarea[name=competitors]", "Rival Waste")
            tab.click("button[type=submit]")
            tab.wait_for_selector("#page-find:not([hidden])")
            assert "4 of 4 searches left this month" in tab.inner_text("#allowance")
            assert tab.inner_text(".co-full") == "Acme Balers"
            tab.click("#settings-link")
            tab.wait_for_selector("#page-settings:not([hidden])")
            assert tab.title() == "Settings · Lead Finder"
            assert tab.input_value("#settings-form input[name=competitors]") == "Rival Waste"
            tab.fill("#settings-form input[name=radius]", "25")
            tab.click("#settings-form button[type=submit]")
            tab.wait_for_selector("#settings-note")
            assert "Your settings are saved." in tab.inner_text("#settings-note")
            assert tab.is_visible("#page-settings")
            assert errors == []
            chromium.close()
    finally:
        server.shutdown()


def test_a_company_connects_to_neon_without_its_pool():
    """Neon's pooled address refuses the search_path option a company's connections use."""
    pooled = "postgresql://u:p@ep-dust-1-pooler.c-3.us-west-2.aws.neon.tech/neondb?sslmode=require"
    assert store.direct_url(pooled) == pooled.replace("-pooler", "")
    assert store.direct_url("host=ep-dust-1-pooler.neon.tech dbname=x") == "host=ep-dust-1.neon.tech dbname=x"
    assert store.direct_url("postgresql://u:p@localhost:5432/x") == "postgresql://u:p@localhost:5432/x"
