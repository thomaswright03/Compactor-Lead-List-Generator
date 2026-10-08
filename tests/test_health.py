"""Every outside source can be seen working without a key: /healthz says whether each is
set up and when it last answered or failed (never a key), `check-sources` asks each for
one small answer (or, with --recorded, runs the real Yelp and Google code against a
local stand-in), `test-alert` posts to the webhook, and `scoring-report` says how the
scoring does on the reference set."""

import html
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from leadgen import alerts, checks, cli, config, health, http, recorded, scoring_report, store, usage, web
from leadgen.sources import SourceError, google_places, yelp

YELP_KEY = "y" * 128
GOOGLE_KEY = "AIza" + "g" * 35


@pytest.fixture(autouse=True)
def fresh_status(monkeypatch):
    health.forget()
    monkeypatch.setattr(http.time, "sleep", lambda s: None)       # no retry backoff
    yield
    health.forget()


def _status(source):
    with store.connect() as db:
        return {row[0]: (row[1], row[2]) for row in
                db.all("SELECT outcome, at, reason FROM source_status WHERE source = ?", (source,))}


# ---- recording: one row per source and outcome, written at most once a minute

def test_an_answer_and_a_failure_are_each_noted_once_a_minute(monkeypatch):
    clock = [1_000.0]
    monkeypatch.setattr(health.time, "time", lambda: clock[0])
    health.record("yelp", True)
    health.record("yelp", True)                   # the same outcome again: not written
    health.record("yelp", False, "api.yelp.com returned HTTP 503 after 1.0s: busy")
    status = _status("yelp")
    assert status["ok"][0] == 1_000.0 and status["failed"][1].startswith("api.yelp.com returned HTTP 503")
    clock[0] = 1_030.0
    health.record("yelp", False, "later")        # within the minute: the first reason stays
    assert _status("yelp")["failed"][1].endswith("busy")
    clock[0] = 1_070.0
    health.record("yelp", False, "later")
    assert _status("yelp")["failed"] == (1_070.0, "later")
    assert not _status("google")


def test_a_note_that_cannot_be_written_never_raises(monkeypatch, caplog):
    def down():
        raise store.Unavailable("the database could not be reached")
    monkeypatch.setattr(store, "open_db", down)
    health.record("osm", True)                    # logged, dropped


def test_every_request_notes_its_source_by_name_or_host(monkeypatch):
    class Answer:
        def __init__(self, status, text):
            self.status_code, self.text, self.headers = status, text, {}

        def json(self):
            return json.loads(self.text)

    answers = [Answer(503, '{"error": "busy"}'), Answer(200, '{"elements": []}')]
    monkeypatch.setattr(http.requests, "request", lambda *a, **k: answers.pop(0))
    http.request_json("POST", config.OVERPASS_ENDPOINTS[0], data={"data": "x"}, use_cache=False)
    osm = _status("osm")
    assert "returned HTTP 503" in osm["failed"][1] and osm["ok"][0] >= osm["failed"][0]
    assert http.source_of("https://api.yelp.com/v3/businesses/search") == "yelp"
    assert http.source_of("https://nominatim.openstreetmap.org/search") == "geocoding"
    assert http.source_of("https://example.com/") is None
    answers.append(Answer(200, '{"businesses": []}'))
    http.request_json("GET", "http://127.0.0.1:1/v3/businesses/search", use_cache=False, source="yelp")
    assert "ok" in _status("yelp")


# ---- /healthz: public, plain, and never a key

def test_healthz_says_what_is_set_up_and_when_each_source_answered(monkeypatch):
    monkeypatch.setenv("YELP_API_KEY", YELP_KEY)
    monkeypatch.setenv("LEADGEN_ALERT_WEBHOOK", "https://hooks.example.com/secret-path")
    monkeypatch.setenv("LEADGEN_GOOGLE_OFF", "1")
    health.record("yelp", True)
    health.record("google", False, "places.googleapis.com returned HTTP 403 after 0.2s: key=REDACTED denied")
    client = web.create_app(password="secret").test_client()
    res = client.get("/healthz")
    body = res.get_json()
    assert res.status_code == 200 and body["ok"] and body["scoring_version"] == config.SCORING_VERSION
    s = body["sources"]
    assert set(s) == set(health.SOURCES)
    assert s["yelp"]["set_up"] and s["yelp"]["last_ok"] and s["yelp"]["last_failure"] is None
    assert s["yelp"]["calls_24h"] == 0 and s["yelp"]["calls_limit"] == config.YELP_DAILY_LIMIT
    assert s["google"] == {"set_up": False, "switched_off": True, "last_ok": None,
                           "last_failure": s["google"]["last_failure"],
                           "failure_reason": "places.googleapis.com returned HTTP 403 after 0.2s: key=REDACTED denied"}
    assert s["google"]["last_failure"]
    assert s["osm"]["set_up"] and s["geocoding"]["set_up"]
    kind = "postgres" if store.database_url() else "sqlite"         # the database this run uses
    assert s["database"]["ok"] and s["database"]["kind"] == kind and s["database"]["last_ok"]
    assert s["alerts"]["set_up"] and s["alerts"]["last_ok"] is None
    text = res.get_data(as_text=True)
    assert YELP_KEY not in text and "secret-path" not in text and "hooks.example.com" not in text


def test_healthz_says_when_the_database_is_down(monkeypatch):
    def down():
        store.last_failure = (time.time(), "OperationalError: connection refused")
        raise store.Unavailable("the database could not be reached")
    monkeypatch.setattr(store, "open_db", down)
    monkeypatch.setattr(store, "last_failure", None)
    body = web.create_app().test_client().get("/healthz").get_json()
    db = body["sources"]["database"]
    assert body["ok"] and not db["ok"] and db["last_ok"] is None
    assert db["last_failure"] and db["failure_reason"] == "OperationalError: connection refused"
    assert body["sources"]["yelp"]["last_ok"] is None


# ---- check-sources: one small call per source, none for a source with no key

def test_without_keys_the_paid_sources_are_not_set_and_not_called(monkeypatch, capsys):
    monkeypatch.setattr(checks, "_osm", lambda: "overpass-api.de answered: 1 map element near AARCO's shop")
    monkeypatch.setattr(checks, "_geocoding", lambda: "Found Salt Lake City, Utah")
    called = []
    monkeypatch.setattr(http.requests, "request", lambda *a, **k: called.append(a))
    assert cli.main(["check-sources"]) == 0
    out = capsys.readouterr().out
    assert "Yelp" in out and "YELP_API_KEY is not set" in out and "GOOGLE_PLACES_API_KEY is not set" in out
    assert "overpass-api.de answered" in out and "Found Salt Lake City" in out
    assert "saved leads" in out and "LEADGEN_ALERT_WEBHOOK is not set" in out
    assert "Every source that is set up answered." in out and not called


def test_with_keys_each_paid_source_gets_one_call_and_yelp_counts_it(monkeypatch, capsys):
    monkeypatch.setenv("YELP_API_KEY", YELP_KEY)
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", GOOGLE_KEY)
    requests_made = []

    def fake(method, url, *, headers=None, params=None, json_body=None, **kw):
        requests_made.append((url, headers, params, json_body))
        if "yelp" in url:
            assert headers["Authorization"] == f"Bearer {YELP_KEY}" and params["limit"] == 50
            return {"businesses": [{"id": "a"}, {"id": "b"}], "total": 2}
        assert headers["X-Goog-Api-Key"] == GOOGLE_KEY and headers["X-Goog-FieldMask"] == "places.id,places.name"
        assert json_body["pageSize"] == 1
        return {"places": [{"id": "g1", "name": "places/g1"}]}
    monkeypatch.setattr(yelp, "request_json", fake)
    monkeypatch.setattr(google_places, "request_json", fake)
    monkeypatch.setattr(checks, "_osm", lambda: "answered")
    monkeypatch.setattr(checks, "_geocoding", lambda: "answered")
    assert cli.main(["check-sources"]) == 0
    out = capsys.readouterr().out
    assert "2 businesses within a mile of AARCO's shop; 49 of 50 calls left" in out
    assert "1 place found near AARCO's shop" in out
    assert len(requests_made) == 2
    # Used up: the Yelp check refuses and spends nothing.
    budget = usage.yelp_budget()
    while budget.take():
        pass
    with pytest.raises(SourceError, match="Not called: this site's 50 Yelp calls for the last 24 hours are used up"):
        yelp.check(YELP_KEY)
    assert len(requests_made) == 2


def test_a_source_that_fails_is_named_and_the_command_exits_2(monkeypatch, capsys):
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", GOOGLE_KEY)

    def refused(*a, **k):
        raise http.HttpError("places.googleapis.com returned HTTP 403 after 0.1s: API_KEY_INVALID", 403)
    monkeypatch.setattr(google_places, "request_json", refused)
    monkeypatch.setattr(checks, "_osm", lambda: "answered")
    monkeypatch.setattr(checks, "_geocoding", lambda: "answered")
    assert cli.main(["check-sources"]) == 2
    printed = capsys.readouterr()
    assert "Google rejected the key. places.googleapis.com returned HTTP 403" in printed.out
    assert "1 source didn't answer." in printed.err


def test_a_switched_off_source_is_not_called(monkeypatch):
    monkeypatch.setenv("YELP_API_KEY", YELP_KEY)
    monkeypatch.setenv("LEADGEN_YELP_OFF", "1")
    monkeypatch.setattr(yelp, "request_json", lambda *a, **k: pytest.fail("called"))
    row = next(c for c in _rows_without_network(monkeypatch) if c.source == "yelp")
    assert row.set_up and row.ok is None and "Switched off by the administrator" in row.detail


def _rows_without_network(monkeypatch):
    monkeypatch.setattr(checks, "_osm", lambda: "answered")
    monkeypatch.setattr(checks, "_geocoding", lambda: "answered")
    return checks.run_all()


# ---- --recorded: the real Yelp and Google code against a local stand-in, no key

def test_recorded_runs_the_real_clients_with_paging_and_a_retry(monkeypatch, capsys):
    monkeypatch.setattr(time, "sleep", lambda s: None)            # Google's page-token wait
    assert cli.main(["check-sources", "--recorded"]) == 0
    out = capsys.readouterr().out
    assert "53 of 53 recorded businesses parsed from 3 requests" in out
    assert "25 of 25 recorded places parsed from 3 requests" in out
    assert out.count("the first answered HTTP 503 and was retried") == 2
    assert "53 with a phone number" in out and "25 with a website" in out
    assert "Not checked with --recorded" in out
    # Nothing of the site's was touched: no Yelp call counted, no status noted.
    assert not _status("yelp") and not _status("google")


def test_the_stand_in_checks_the_key_like_the_real_apis():
    with recorded.serving() as server:
        with pytest.raises(SourceError, match="Yelp rejected the request"):
            yelp.search(40.74, -111.94, 5, ["grocery stores"], "wrong-key", max_requests=2)
        with pytest.raises(SourceError, match="Google rejected the request"):
            google_places.search(40.74, -111.94, 5, ["grocery store"], "wrong-key", max_requests=2)
        assert server.requests == {"yelp": 1, "google": 1}
    assert yelp.SEARCH_URL.startswith("https://api.yelp.com/")      # put back
    assert google_places.SEARCH_URL.startswith("https://places.googleapis.com/")


# ---- test-alert: a message to the webhook, and /healthz says when one was last delivered

class _Hook(BaseHTTPRequestHandler):
    status = 200
    got = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        _Hook.got.append((self.headers.get("Content-Type"), json.loads(body)))
        self.send_response(_Hook.status)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


@pytest.fixture
def hook(monkeypatch):
    server = HTTPServer(("127.0.0.1", 0), _Hook)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _Hook.got, _Hook.status = [], 200
    monkeypatch.setenv(alerts.WEBHOOK_ENV, f"http://127.0.0.1:{server.server_address[1]}/hook")
    yield _Hook
    server.shutdown()
    server.server_close()
    thread.join(5)


def test_test_alert_posts_to_the_webhook_and_is_noted(hook, capsys):
    assert cli.main(["test-alert"]) == 0
    [(content_type, body)] = hook.got
    assert content_type == "application/json" and body["text"] == body["content"]
    assert body["text"].startswith(f"{alerts.SITE}: Test alert sent from the command line at ")
    assert "Delivered to the webhook" in capsys.readouterr().out
    status = web.create_app().test_client().get("/healthz").get_json()["sources"]["alerts"]
    assert status["set_up"] and status["last_ok"] and status["last_failure"] is None


def test_a_webhook_that_refuses_is_reported_and_noted(hook, capsys):
    hook.status = 500
    assert cli.main(["test-alert"]) == 2
    assert "Not delivered: HTTPError: HTTP Error 500" in capsys.readouterr().err
    status = health.status()["alerts"]
    assert status["last_failure"] and status["failure_reason"].startswith("HTTPError: HTTP Error 500")
    assert "127.0.0.1" not in status["failure_reason"]


def test_without_a_webhook_test_alert_says_so(capsys):
    assert cli.main(["test-alert"]) == 2
    assert "No alert webhook is set up (LEADGEN_ALERT_WEBHOOK is not set)" in capsys.readouterr().err


def test_a_plain_http_webhook_elsewhere_is_still_refused():
    with pytest.raises(ValueError, match="must start with https://"):
        alerts._post("http://hooks.example.com/plain", "x")


# ---- the administrator's buttons do the same

def _admin():
    client = web.create_app(password="s3cret", username="Matt", admin_password="open-sesame").test_client()
    client.post("/login", data={"username": "Matt", "password": "s3cret"})
    return client


def _page(response):
    """The page's text as read (a form's answer is a page, with its characters unescaped)."""
    return html.unescape(response.get_data(as_text=True))


def test_the_buttons_need_the_section_unlocked(monkeypatch):
    monkeypatch.setattr(checks, "run_all", lambda recorded=False: pytest.fail("ran"))
    client = _admin()
    for path in ("/admin/check-sources", "/admin/test-alert"):
        res = client.post(path)
        assert res.status_code == 403 and "Unlock the administrator's section" in _page(res)
        elsewhere = client.post(path, headers={"Sec-Fetch-Site": "cross-site"})
        assert elsewhere.status_code == 403 and "can't be opened from here" in _page(elsewhere)


def test_check_sources_shows_the_table(monkeypatch):
    rows = [checks.Check("yelp", True, True, 0.4, "2 businesses within a mile of AARCO's shop"),
            checks.Check("google", False, None, None, "GOOGLE_PLACES_API_KEY is not set"),
            checks.Check("osm", True, False, 9.0, "No map server answered: busy")]
    monkeypatch.setattr(checks, "run_all", lambda recorded=False: rows)
    client = _admin()
    client.post("/admin/unlock", json={"password": "open-sesame"})
    page = _page(client.post("/admin/check-sources"))
    assert "Source check" in page and "1 of the sources that are set up didn't answer." in page
    assert "2 businesses within a mile" in page and "No map server answered: busy" in page
    assert "Map data (OpenStreetMap)" in page and 'href="/#find"' in page


def test_send_a_test_alert_from_the_site(hook):
    client = _admin()
    client.post("/admin/unlock", json={"password": "open-sesame"})
    page = _page(client.post("/admin/test-alert"))
    assert "Delivered. The message was:" in page and "Test alert sent from the site at" in page
    assert len(hook.got) == 1
    hook.status = 404
    page = _page(client.post("/admin/test-alert"))
    assert "couldn't be delivered: HTTPError: HTTP Error 404" in page


# ---- scoring-report: the scorer's quality on the reference set, with its rules' version

def test_scoring_report_counts_each_tier_and_names_moved_businesses(tmp_path, capsys):
    assert cli.main(["scoring-report"]) == 0
    out = capsys.readouterr().out
    assert f"Scoring rules version {config.SCORING_VERSION}" in out
    assert "Tier  Businesses  Have equipment  Don't  Flagged" in out
    assert "the tests need 75%" in out and "None yet: the site's Yes / No marks" in out
    path = tmp_path / "ref.json"
    path.write_text(json.dumps({"businesses": [], "confirmed": [
        {"name": "Harmons Grocery", "source": "google", "raw_categories": ["supermarket", "grocery_store"],
         "rating_count": 2400, "marked": "yes", "tier": "A"},
        {"name": "A Nail Salon", "source": "google", "raw_categories": ["nail_salon"],
         "marked": "no", "tier": "B"}]}))
    text = scoring_report.report(path)
    assert "A Nail Salon (marked no): B -> D" in text and "Harmons" not in text.split("Moved tier")[1]
    assert "Moved tier since the file was copied:" in text
    assert cli.main(["scoring-report", "--reference", str(tmp_path / "missing.json")]) == 2
