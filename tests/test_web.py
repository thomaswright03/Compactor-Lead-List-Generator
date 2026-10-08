import time

from leadgen import web
from leadgen.models import Lead
from leadgen.pipeline import RunResult


def test_web_flow(monkeypatch):
    lead = Lead(name="<b>Walmart</b>", lat=40.7, lon=-111.9, source="google", source_id="x",
                score=70, tier="A", category="Grocery / supermarket", distance_miles=3.2)
    monkeypatch.setattr(web.finding, "run", lambda params, progress: RunResult(
        [lead], (40.76, -111.89), "Salt Lake City, UT", [], {"leads kept": 1}))
    client = web.create_app().test_client()
    assert b"Lead Finder" in client.get("/").data
    job = client.post("/search", data={"location": "84101", "radius": "30"}).get_json()["job_id"]
    body = _finished(client, job)
    assert body["state"] == "done" and body["found"] == 1 and body["saved"]
    assert client.get(f"/download/{job}.csv").status_code == 200
    assert client.get(f"/download/{job}.xlsx").data[:2] == b"PK"
    assert client.get(f"/download/{job}.pdf").status_code == 404


def _finished(client, job):
    """The search's status once it has ended (a test waits for the searches it starts)."""
    for _ in range(200):
        body = client.get(f"/status/{job}").get_json()
        if body["state"] != "running":
            return body
        time.sleep(0.05)
    raise AssertionError("the search never finished")


def _client(monkeypatch, block=None):
    import threading
    gate = threading.Event()

    def fake_run(params, progress):
        if block:
            gate.wait(5)
        return RunResult([], (40.76, -111.89), "SLC", [], {})
    monkeypatch.setattr(web.finding, "run", fake_run)
    return web.create_app().test_client(), gate


def test_web_rejects_bad_input(monkeypatch):
    client, _ = _client(monkeypatch)
    for form in ({"grid": "2"}, {"radius": "abc"}, {"radius": "500"}, {"max_requests": "999999"},
                 {"source": "evil"}, {"keywords": ",".join(["k"] * 30)}):
        res = client.post("/search", data=form)
        assert res.status_code == 400 and res.get_json()["error"]


def test_web_blocks_cross_site_posts(monkeypatch):
    client, _ = _client(monkeypatch)
    res = client.post("/search", data={}, headers={"Origin": "https://evil.example"})
    assert res.status_code == 403
    res = client.post("/search", data={}, headers={"Sec-Fetch-Site": "cross-site"})
    assert res.status_code == 403


def test_web_allows_one_running_search(monkeypatch):
    client, gate = _client(monkeypatch, block=True)
    first = client.post("/search", data={})
    assert first.status_code == 200
    second = client.post("/search", data={})
    assert second.status_code == 429
    assert second.get_json()["job_id"] == first.get_json()["job_id"]
    gate.set()
    assert _finished(client, first.get_json()["job_id"])["state"] == "done"


def test_web_blocks_dns_rebinding_without_password(monkeypatch):
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    client, _ = _client(monkeypatch)
    res = client.post("/search", data={}, base_url="http://rebind.attacker.example:5000",
                      headers={"Origin": "http://rebind.attacker.example:5000",
                               "Sec-Fetch-Site": "same-origin"})
    assert res.status_code == 403
    for host in ("http://localhost:5000", "http://127.0.0.1:5000", "http://[::1]:5000"):
        assert client.get("/", base_url=host).status_code == 200


def test_web_accepts_browser_number_formats(monkeypatch):
    client, gate = _client(monkeypatch)
    res = client.post("/search", data={"min_score": "20.0", "max_requests": "1e3", "radius": "30"})
    assert res.status_code == 200
    assert client.post("/search", data={"min_score": "20.5"}).status_code in (400, 429)
    assert _finished(client, res.get_json()["job_id"])["state"] == "done"


def _login(client, username="Matt", password="s3cret"):
    return client.post("/login", data={"username": username, "password": password})


def test_login_page_protects_every_route():
    client = web.create_app(password="s3cret", username="Matt").test_client()
    assert client.get("/").headers["Location"].endswith("/login")
    assert client.get("/download/saved.csv").status_code == 302
    assert client.post("/search", data={}).status_code == 401
    assert client.get("/leads").status_code == 401
    page = client.get("/login")
    assert page.status_code == 200 and b"Wright AI Solutions" in page.data
    assert _login(client, password="wrong").status_code == 401
    assert _login(client, username="Bob").status_code == 401
    assert client.get("/leads").status_code == 401
    assert _login(client, username="matt").status_code == 401    # case-sensitive
    res = _login(client)
    assert res.status_code == 302 and res.headers["Location"] == "/"
    home = client.get("/")
    assert home.status_code == 200 and b"Wright AI Solutions" in home.data
    assert b"Matt" in home.data and b"Log out" in home.data
    assert client.get("/leads").status_code == 200
    client.post("/logout")
    assert client.get("/leads").status_code == 401


def test_changing_the_password_ends_logins():
    old = web.create_app(password="s3cret", username="Matt")
    client = old.test_client()
    _login(client)
    assert client.get("/leads").status_code == 200
    new = web.create_app(password="n3w", username="Matt")
    cookie = client.get_cookie("session")
    other = new.test_client()
    other.set_cookie("session", cookie.value)
    assert other.get("/leads").status_code == 401


def test_wrong_passwords_are_throttled(monkeypatch):
    monkeypatch.setattr(web.time, "sleep", lambda s: None)
    client = web.create_app(password="s3cret", username="Matt").test_client()
    for _ in range(web.LOGIN_TRIES):
        assert _login(client, password="nope").status_code == 401
    assert _login(client).status_code == 429           # even the right one, for a while


def test_login_rejects_other_sites():
    client = web.create_app(password="s3cret", username="Matt").test_client()
    res = client.post("/login", data={"username": "Matt", "password": "s3cret"},
                      headers={"Sec-Fetch-Site": "cross-site"})
    assert res.status_code == 403


def test_no_password_means_open(monkeypatch):
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    assert web.create_app().test_client().get("/").status_code == 200


def test_healthz_is_public_and_shows_the_deployed_commit(monkeypatch):
    monkeypatch.setenv("RENDER_GIT_COMMIT", "abcdef1234567")
    client = web.create_app(password="secret").test_client()
    res = client.get("/healthz", headers={"Host": "evil.example"})
    body = res.get_json()
    assert res.status_code == 200 and body["ok"] is True and body["version"] == "abcdef1"
    # Nothing but the site's standing (tests/test_health.py): it needs no login.
    assert set(body) == {"ok", "version", "up_since", "scoring_version", "sources"}
    assert client.get("/").status_code == 302


def test_a_refused_form_names_the_field(monkeypatch):
    client, _ = _client(monkeypatch)
    for form, field in [({"keywords": "x" * 61}, "keywords"), ({"keywords": ",".join(["k"] * 21)}, "keywords"),
                        ({"location": "   "}, "location"), ({"radius": "500"}, "radius"),
                        ({"min_score": "abc"}, "min_score"), ({"source": "evil"}, "source")]:
        res = client.post("/search", data=form)
        body = res.get_json()
        assert res.status_code == 400 and body["field"] == field and body["error"], form
    assert "up to 60 characters" in client.post("/search", data={"keywords": "x" * 61}).get_json()["error"]


def test_the_finished_search_sends_counts_not_the_saved_list(monkeypatch):
    from leadgen import saved
    many = [Lead(name=f"Store {i}", lat=40.7 + i * 0.001, lon=-111.9, source="osm", source_id=f"n{i}",
                 score=50, tier="B", reasons=["+30 Grocery / supermarket"] * 3) for i in range(1500)]
    saved.save_search(many)
    found = [Lead(name="Walmart", lat=40.6, lon=-111.8, source="google", source_id="w", score=70, tier="A")]
    monkeypatch.setattr(web.finding, "run", lambda params, progress: RunResult(
        found, (40.76, -111.89), "Salt Lake City, UT", [], {"leads kept": 1}))
    client = web.create_app().test_client()
    job = client.post("/search", data={"location": "84101"}).get_json()["job_id"]
    for _ in range(200):
        res = client.get(f"/status/{job}")
        if res.get_json()["state"] != "running":
            break
        time.sleep(0.05)
    body = res.get_json()
    assert body["saved"] and body["new_leads"] == 1 and body["saved_count"] == 1501 and body["found"] == 1
    assert "leads" not in body and len(res.data) < 5000


def test_after_an_ended_login_the_login_page_says_so_and_goes_back_to_the_same_view(monkeypatch):
    """core.js sends a page whose login ended to /login?ended=1&next=<its path and #address>:
    the page says what may not have been saved, and logging in goes back there."""
    monkeypatch.setattr(web.time, "sleep", lambda s: None)
    client = web.create_app(password="s3cret", username="Matt").test_client()
    page = client.get("/login?ended=1&next=%2F%23leads%3Ftab%3Dyes%26q%3Dsmith").data.decode()
    assert web.auth.ENDED.replace("'", "&#39;") in page
    assert '<input type="hidden" name="next" value="/#leads?tab=yes&amp;q=smith">' in page
    assert '<input type="hidden" name="ended" value="1">' in page
    assert 'id="ended"' not in client.get("/login").data.decode()           # an ordinary visit
    # A wrong password keeps both.
    form = {"username": "Matt", "password": "no", "next": "/#leads?tab=yes&q=smith", "ended": "1"}
    wrong = client.post("/login", data=form).data.decode()
    assert 'value="/#leads?tab=yes&amp;q=smith"' in wrong and 'id="ended"' in wrong
    res = client.post("/login", data={**form, "password": "s3cret"})
    assert res.status_code == 302 and res.headers["Location"] == "/#leads?tab=yes&q=smith"
    # Already logged in: straight there.
    again = client.get("/login?next=%2F%23calls")
    assert again.status_code == 302 and again.headers["Location"] == "/#calls"


def test_logging_in_never_sends_anyone_to_another_site(monkeypatch):
    monkeypatch.setattr(web.time, "sleep", lambda s: None)
    for target in ("https://evil.example/", "//evil.example/x", "/\\evil.example", "\\\\evil.example",
                   "javascript:alert(1)", "evil.example", "/login", "/logout", "/x\r\nSet-Cookie: a=b",
                   "http:/evil.example", "/" + "a" * 3000):
        assert web.auth.return_to(target) == "", target
        client = web.create_app(password="s3cret", username="Matt").test_client()
        assert 'name="next"' not in client.get("/login", query_string={"next": target}).data.decode()
        res = client.post("/login", data={"username": "Matt", "password": "s3cret", "next": target})
        assert res.status_code == 302 and res.headers["Location"] == "/", target
    for target in ("/", "/#leads", "/#leads?tab=competitors&tier=A", "/#find"):
        assert web.auth.return_to(target) == target
