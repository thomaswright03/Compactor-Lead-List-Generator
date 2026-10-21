"""The administrator's page, /admin: no account login, just the administrator password."""

import pytest

from leadgen import switches, web
from leadgen.web import admin

ORIGIN = {"Origin": "http://localhost"}


@pytest.fixture(params=["aarco", "signup"])
def client(request, monkeypatch):
    if request.param == "signup":
        monkeypatch.setenv("LEADGEN_SIGNUP", "1")
        monkeypatch.setenv("SECRET_KEY", "tests-only-secret")
    return web.create_app(password="s3cret", username="Matt", admin_password="adm1n").test_client()


def test_the_page_opens_without_a_login_and_unlocks_with_its_password(client):
    page = client.get("/admin", headers={"Accept": "text/html"})
    assert page.status_code == 200 and 'name="password"' in page.get_data(as_text=True)
    assert client.post("/admin/check-sources", headers=ORIGIN).status_code == 403    # locked
    assert client.get("/download/scoring-reference.json").status_code == 403
    wrong = client.post("/admin", data={"password": "nope"}, headers=ORIGIN)
    assert wrong.status_code == 403 and "Wrong password." in wrong.get_data(as_text=True)
    assert client.post("/admin", data={"password": "adm1n"},
                       headers={"Origin": "https://elsewhere.example"}).status_code == 403
    res = client.post("/admin", data={"password": "adm1n"}, headers=ORIGIN)
    assert res.status_code == 302 and res.headers["Location"].endswith("/admin")
    text = client.get("/admin").get_data(as_text=True)
    assert "Recent problems" in text and "Site switches" in text and "Lock this page" in text
    assert "Nothing went wrong in the last 7 days." in text
    # A switch, from its form.
    assert client.post("/admin/switch", data={"key": "search_paused", "on": "1"}, headers=ORIGIN).status_code == 302
    assert switches.state()["search_paused"]["on"]
    assert "Saved. It works at once." in client.get("/admin").get_data(as_text=True)
    client.post("/admin/switch", data={"key": "search_paused", "on": "0"}, headers=ORIGIN)
    assert not switches.state()["search_paused"]["on"]
    assert client.get("/download/scoring-reference.json").status_code == 200
    # Lock it again.
    client.post("/admin/relock", headers=ORIGIN)
    assert 'name="password"' in client.get("/admin").get_data(as_text=True)


def test_no_admin_password_says_how_to_set_one(monkeypatch):
    page = web.create_app(password="s3cret", username="Matt", admin_password="").test_client().get("/admin")
    assert "ADMIN_PASSWORD" in page.get_data(as_text=True) and 'name="password"' not in page.get_data(as_text=True)


def test_the_rest_of_the_site_still_needs_a_login(client):
    assert client.get("/leads").status_code == 401
    assert admin.bp.name == "admin"


def test_the_page_is_valid_html_locked_and_open(client):
    from tests.test_html_valid import html_problems
    assert html_problems(client.get("/admin").get_data(as_text=True)) == []
    client.post("/admin", data={"password": "adm1n"}, headers=ORIGIN)
    assert html_problems(client.get("/admin").get_data(as_text=True)) == []
