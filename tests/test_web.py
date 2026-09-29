import time

from leadgen import web
from leadgen.models import Lead
from leadgen.pipeline import RunResult


def test_web_flow(monkeypatch):
    lead = Lead(name="<b>Walmart</b>", lat=40.7, lon=-111.9, source="google", source_id="x",
                score=70, tier="A", category="Grocery / supermarket", distance_miles=3.2)
    monkeypatch.setattr(web, "run", lambda params, progress: RunResult(
        [lead], (40.76, -111.89), "Salt Lake City, UT", [], {"leads kept": 1}))
    client = web.create_app().test_client()
    assert b"Lead Finder" in client.get("/").data
    job = client.post("/search", data={"location": "84101", "radius": "30"}).get_json()["job_id"]
    for _ in range(50):
        body = client.get(f"/status/{job}").get_json()
        if body["state"] != "running":
            break
        time.sleep(0.05)
    assert body["state"] == "done" and body["leads"][0]["name"] == "<b>Walmart</b>"
    assert client.get(f"/download/{job}.csv").status_code == 200
    assert client.get(f"/download/{job}.xlsx").data[:2] == b"PK"
    assert client.get(f"/download/{job}.pdf").status_code == 404


def _client(monkeypatch, block=None):
    import threading
    gate = threading.Event()

    def fake_run(params, progress):
        if block:
            gate.wait(5)
        return RunResult([], (40.76, -111.89), "SLC", [], {})
    monkeypatch.setattr(web, "run", fake_run)
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
    assert res.status_code == 200 and res.get_json() == {"ok": True, "version": "abcdef1"}
    assert client.get("/").status_code == 302
