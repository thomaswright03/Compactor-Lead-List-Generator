"""The demo account (demo.py): "test" / "test" logs in to a made-up company with leads,
marks, calls and searches, and can't change the company, invite anyone or search."""

from leadgen import demo, web
from tests.test_signup import _page, signup  # noqa: F401 - fixture


def _client():
    client = web.create_app().test_client()
    res = client.post("/login", data={"email": "TEST ", "password": "test"})
    assert res.status_code == 302, res.get_data(as_text=True)
    return client


def test_the_demo_account_logs_in_to_its_made_up_company(signup):  # noqa: F811
    client = _client()                                        # made at the first login as "test"
    assert not demo.ensure()                                  # and only once
    assert _page(client).status_code == 200
    leads = client.get("/leads").get_json()["leads"]
    assert len(leads) == len(demo.BUSINESSES)
    assert all("555-01" in l["phone"] for l in leads)
    assert sum(1 for l in leads if l["has_baler"] == "yes") >= 20
    assert sum(1 for l in leads if l.get("call_outcome")) >= 15
    assert len(client.get("/searches").get_json()["searches"]) >= 10
    assert client.get("/stats").status_code == 200
    # A member: no Settings, invites or billing; and no searching.
    headers = {"Origin": "http://localhost"}
    assert client.post("/settings", data={"name": "X"}, headers=headers).status_code == 403
    assert client.post("/settings/invite", data={"email": "a@b.example"}, headers=headers).status_code == 403
    res = client.post("/search", data={"location": "84101"}, headers=headers)
    assert res.status_code >= 400 and "demo" in res.get_json()["error"]


def test_a_wrong_password_or_other_word_does_not_log_in(signup):  # noqa: F811
    demo.ensure()
    client = web.create_app().test_client()
    for email, password in (("test", "wrong"), ("demo", "test"), ("", "test")):
        assert client.post("/login", data={"email": email, "password": password}).status_code == 401
