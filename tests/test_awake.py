"""The live site notices when it went to sleep during Utah working hours (the keep-awake
pinger stopped) and says so under Recent problems, once a day; each start gets the
first page ready in the background; /healthz says since when the site has been up."""

import datetime as dt
import threading

import pytest

from leadgen import alerts, awake, saved, store, web
from leadgen.localtime import UTAH
from leadgen.models import Lead


def _utah(day, hour, minute=0):
    return dt.datetime(2026, 10, day, hour, minute, tzinfo=UTAH).timestamp()


@pytest.fixture
def reporting(monkeypatch):
    monkeypatch.setattr(alerts, "ON", True)


def _problems():
    with store.connect() as db:
        return [text for (text,) in db.all("SELECT text FROM problems ORDER BY at")]


def test_a_start_in_working_hours_without_a_deploy_means_the_site_had_slept(reporting):
    assert awake.check_start("abc1234", _utah(5, 6, 1)) is None          # the first start on record
    assert awake.check_start("abc1234", _utah(5, 6, 15)) is None         # the morning's first ping woke it
    said = awake.check_start("abc1234", _utah(5, 9, 32))                 # asleep after that: the pinger stopped
    assert said and said.startswith("The site had gone to sleep and started again at Oct 5, 2026, 9:32 AM")
    assert "keep-awake pinger" in said and _problems() == [said]
    # Said once a Utah day; again the next day.
    assert awake.check_start("abc1234", _utah(5, 14, 0)) is None
    assert awake.check_start("abc1234", _utah(6, 10, 0))
    assert len(_problems()) == 2


def test_a_deploy_or_a_start_outside_working_hours_is_no_problem(reporting):
    awake.check_start("abc1234", _utah(5, 8))
    assert awake.check_start("def5678", _utah(5, 11)) is None             # a new version: a deploy
    assert awake.check_start("def5678", _utah(5, 22, 30)) is None         # after 9 PM
    assert awake.check_start("def5678", _utah(6, 5, 50)) is None          # before 6 AM
    assert awake.check_start("def5678", _utah(6, 6, 19)) is None          # the first ping of the day
    assert _problems() == []
    with store.connect() as db:
        assert db.one("SELECT COUNT(*) FROM site_starts")[0] == 5


def test_each_start_gets_the_saved_list_ready_in_the_background(monkeypatch):
    saved.save_search([Lead(name="Costco", lat=40.7, lon=-111.9, source="osm", source_id="c",
                            raw_categories=["shop=wholesale"])])
    saved._parsed.clear()
    checked = []
    monkeypatch.setattr(awake, "check_start", lambda version, at=None: checked.append(version))
    awake.start("abc1234").join(10)
    assert checked == ["abc1234"] and saved._parsed                       # parsed once, kept
    # Not on Render (no version): no check, the list is still made ready.
    saved._parsed.clear()
    awake.start("").join(10)
    assert checked == ["abc1234"] and saved._parsed


def test_a_failing_check_never_stops_the_site(monkeypatch, caplog):
    def down(*a, **k):
        raise store.Unavailable("the database could not be reached")
    monkeypatch.setattr(awake, "check_start", down)
    monkeypatch.setattr(awake, "warm_up", down)
    awake.start("abc1234").join(10)
    assert "Checking whether the site had been asleep failed" in caplog.text
    assert "Getting the saved list ready failed" in caplog.text


def test_one_parse_of_the_saved_list_serves_requests_that_overlap(monkeypatch):
    saved.save_search([Lead(name="Costco", lat=40.7, lon=-111.9, source="osm", source_id="c",
                            raw_categories=["shop=wholesale"])])
    saved._parsed.clear()
    parsed = []
    real = saved._ready
    gate = threading.Event()

    def slow(row):
        parsed.append(row.uid)
        gate.wait(5)
        return real(row)
    monkeypatch.setattr(saved, "_ready", slow)
    threads = [threading.Thread(target=saved.load) for _ in range(3)]
    for t in threads:
        t.start()
    gate.set()
    for t in threads:
        t.join(10)
    assert len(parsed) == 1


def test_healthz_says_since_when_the_site_is_up():
    body = web.create_app().test_client().get("/healthz").get_json()
    assert body["ok"] and body["up_since"]
