"""Yes / No "has a baler" marks on the results page."""

import time

from leadgen import marks, usage, web
from leadgen.export import COLUMNS, to_csv_bytes
from leadgen.models import Lead
from leadgen.pipeline import RunResult

from test_usage import FakeRedis


class FakeRedisWithHash(FakeRedis):
    def hset(self, key, field, value):
        self._check()
        self.data.setdefault(key, {})[field] = value

    def hdel(self, key, field):
        self._check()
        self.data.get(key, {}).pop(field, None)

    def hmget(self, key, fields):
        self._check()
        return [self.data.get(key, {}).get(f) for f in fields]


def _lead(name="Smith's Marketplace", **kw):
    base = dict(lat=40.75, lon=-111.87, source="yelp", source_id="a", phone="+1 801-555-0100",
                zip="84102", score=70, tier="A")
    base.update(kw)
    return Lead(name=name, **base)


def test_key_is_the_same_business_across_sources():
    a = _lead(source="yelp", phone="(801) 555-0100", lat=40.7501)
    b = _lead(name="SMITHS MARKETPLACE", source="google", phone="+18015550100", lat=40.7507)
    assert marks.lead_key(a) == marks.lead_key(b)
    assert marks.lead_key(_lead(phone="")) != marks.lead_key(_lead(phone="", zip="84101"))
    assert marks.lead_key(_lead(name="Other Store")) != marks.lead_key(a)


def test_marks_saved_cleared_and_exported():
    lead, other = _lead(), _lead(name="Costco")
    marks.set_mark(marks.lead_key(lead), "yes", lead.name)
    marks.set_mark(marks.lead_key(other), "no", other.name)
    marks.apply([lead, other])
    assert (lead.has_baler, other.has_baler) == ("yes", "no")
    csv = to_csv_bytes([lead, other]).decode("utf-8-sig")
    assert "Has Baler?" in [c for c, _, _ in COLUMNS]
    assert ",Yes," in csv and ",No," in csv
    marks.set_mark(marks.lead_key(lead), "", lead.name)
    assert marks.apply([lead])[0].has_baler == ""


def test_marks_live_in_redis_when_set(monkeypatch):
    r = FakeRedisWithHash()
    monkeypatch.setattr(usage, "redis_client", lambda: r)
    lead = _lead()
    marks.set_mark(marks.lead_key(lead), "no", lead.name)
    assert marks.lead_key(lead) in r.data[marks.HASH_KEY]
    assert marks.apply([lead])[0].has_baler == "no"


def test_page_marks_survive_a_new_search(monkeypatch):
    lead = _lead(name="Walmart")
    monkeypatch.setattr(web, "run", lambda params, progress: RunResult(
        [_lead(name="Walmart")], (40.76, -111.89), "SLC", [], {}))
    client = web.create_app().test_client()

    def search():
        job = client.post("/search", data={"location": "84101"}).get_json()["job_id"]
        for _ in range(50):
            body = client.get(f"/status/{job}").get_json()
            if body["state"] != "running":
                return job, body
            time.sleep(0.05)

    job, body = search()
    row = body["leads"][0]
    assert row["has_baler"] == "" and row["key"] == marks.lead_key(lead)
    assert client.post("/mark", json={"key": row["key"], "value": "yes"}).get_json() == {"ok": True}
    assert "Yes" in client.get(f"/download/{job}.csv").data.decode("utf-8-sig")
    _, body = search()                                  # a later search shows the mark again
    assert body["leads"][0]["has_baler"] == "yes"


def test_mark_endpoint_checks_input(monkeypatch):
    client = web.create_app().test_client()
    assert client.post("/mark", json={"key": "k", "value": "maybe"}).status_code == 400
    assert client.post("/mark", json={"value": "yes"}).status_code == 400
    assert client.post("/mark", json={"key": "k", "value": "yes"},
                       headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    monkeypatch.setenv("RENDER", "true")                # no Key Value store connected
    res = client.post("/mark", json={"key": "k", "value": "yes"})
    assert res.status_code == 503 and "Key Value store" in res.get_json()["error"]
