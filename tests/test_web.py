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
    assert client.post("/search", data={}).status_code == 429
    gate.set()


def test_password_protects_every_route():
    import base64
    client = web.create_app(password="s3cret").test_client()
    assert client.get("/").status_code == 401
    assert client.post("/search", data={}).status_code == 401
    bad = base64.b64encode(b"any:wrong").decode()
    assert client.get("/", headers={"Authorization": f"Basic {bad}"}).status_code == 401
    good = base64.b64encode(b"arco:s3cret").decode()
    assert client.get("/", headers={"Authorization": f"Basic {good}"}).status_code == 200


def test_no_password_means_open(monkeypatch):
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    assert web.create_app().test_client().get("/").status_code == 200
