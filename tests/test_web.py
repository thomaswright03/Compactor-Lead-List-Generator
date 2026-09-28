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
