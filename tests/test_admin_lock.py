"""The administrator's section (problems, switches, scoring check) has its own password."""

from leadgen import web


def _client(admin="open-sesame"):
    client = web.create_app(password="s3cret", username="Matt", admin_password=admin).test_client()
    client.post("/login", data={"username": "Matt", "password": "s3cret"})
    return client


def test_the_section_is_locked_until_its_password_is_given():
    client = _client()
    page = client.get("/").get_data(as_text=True)
    assert 'id="admin-pass"' in page and "Enter the administrator password" in page
    assert client.post("/switches", json={"key": "search_paused", "on": True}).status_code == 403
    assert client.get("/download/scoring-reference.json").status_code == 403
    body = client.get("/searches").get_json()
    assert body["admin"] == "locked" and body["problems"] is None
    assert body["switches"]["search_paused"]["on"] is False       # still shown, not changeable

    wrong = client.post("/admin/unlock", json={"password": "nope"})
    assert wrong.status_code == 403 and wrong.get_json()["error"] == "Wrong password."
    assert client.post("/admin/unlock", json={"password": "open-sesame"}).get_json() == {"admin": "open"}
    assert client.get("/searches").get_json()["admin"] == "open"
    assert client.post("/switches", json={"key": "search_paused", "on": True}).status_code == 200
    client.post("/switches", json={"key": "search_paused", "on": False})
    assert client.get("/download/scoring-reference.json").status_code == 200

    client.post("/admin/lock")
    assert client.post("/switches", json={"key": "search_paused", "on": True}).status_code == 403


def test_other_logins_stay_locked_and_a_new_password_locks_everyone():
    first = _client()
    first.post("/admin/unlock", json={"password": "open-sesame"})
    assert _client().get("/searches").get_json()["admin"] == "locked"   # another device
    # Changing the admin password locks the section again for everyone already in.
    first.application.extensions["leadgen"].admin_password = "changed"
    assert first.get("/searches").get_json()["admin"] == "locked"


def test_wrong_tries_are_limited(monkeypatch):
    monkeypatch.setattr(web.auth.time, "sleep", lambda s: None)
    client = _client()
    for _ in range(10):
        client.post("/admin/unlock", json={"password": "nope"})
    too_many = client.post("/admin/unlock", json={"password": "open-sesame"})
    assert too_many.status_code == 429


def test_without_an_admin_password_a_live_site_keeps_it_locked_and_a_local_copy_open():
    client = _client(admin="")
    assert "adds an administrator password (ADMIN_PASSWORD)" in client.get("/").get_data(as_text=True)
    assert client.post("/admin/unlock", json={"password": "x"}).status_code == 400
    assert client.post("/switches", json={"key": "search_paused", "on": True}).status_code == 403
    local = web.create_app(admin_password="").test_client()
    assert local.get("/searches").get_json()["admin"] == "open"


def test_an_unreadable_database_is_never_an_all_clear(monkeypatch):
    from leadgen import alerts, switches

    client = _client()
    client.post("/admin/unlock", json={"password": "open-sesame"})
    body = client.get("/searches").get_json()
    assert body["problems"] == {"count": 0, "latest": []} and body["problems_unread"] is False
    assert not any(s["unread"] for s in body["switches"].values())

    def down(*args, **kw):
        raise ConnectionError("the database isn't answering")
    monkeypatch.setattr(alerts, "recent", down)
    monkeypatch.setattr(switches.store, "connect", down)
    body = client.get("/searches")
    # The history itself needs the database too: the page is told it couldn't load.
    assert body.status_code == 503
    monkeypatch.setattr("leadgen.web.finding.daily.history",
                        lambda: {"today": "2026-09-30", "used_today": False, "current": None,
                                 "searches": [], "reruns_left": None})
    body = client.get("/searches").get_json()
    assert body["problems"] is None and body["problems_unread"] is True
    assert all(s["unread"] for s in body["switches"].values())
    assert body["switches"]["search_paused"]["on"] is False
