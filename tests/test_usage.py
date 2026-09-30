"""The website's hard daily limit on Yelp calls (config.YELP_DAILY_LIMIT)."""

import pytest
from test_yelp import YELP_KEY, _fake_yelp

from leadgen import config, http, store, usage, web
from leadgen.localtime import day_clock_text
from leadgen.sources import yelp


def _search(queries, **kw):
    return yelp.search(40.76, -111.89, 20, queries, YELP_KEY, **kw)


def test_limit_is_50_and_the_default_cap_respects_it():
    assert config.YELP_DAILY_LIMIT == 50
    assert config.YELP_DEFAULT_MAX_REQUESTS <= config.YELP_DAILY_LIMIT


def test_limit_counts_across_searches_and_no_cap_overrides_it(monkeypatch):
    calls = _fake_yelp(monkeypatch, total=10)
    _, n1, _ = _search([f"a{i}" for i in range(30)])
    _, n2, w2 = _search([f"b{i}" for i in range(30)], max_requests=5000)
    assert (n1, n2, len(calls)) == (30, 20, 50)
    assert any("Stopped after 20 Yelp calls (this site may make 50 Yelp calls in any 24 hours "
               "and 20 were left" in w for w in w2)
    assert usage.yelp_budget().left() == 0
    # Used up: cached searches still come back, new ones make no calls.
    leads, n3, w3 = _search(["a0", "c0"])
    assert n3 == 0 and len(calls) == 50 and len(leads) == 10
    assert any("c0" in w and "Not searched at all" in w for w in w3)


def test_retries_count_as_calls(monkeypatch):
    budget = usage.yelp_budget()
    for _ in range(48):
        assert budget.take()
    attempts = []

    class Busy:
        status_code, text, headers = 503, "busy", {}

    monkeypatch.setattr(http.requests, "request", lambda *a, **k: attempts.append(1) or Busy())
    monkeypatch.setattr(http.time, "sleep", lambda s: None)
    _, n, warnings = _search(["a", "b"])
    assert len(attempts) == 2 and budget.left() == 0      # the first try and one retry
    assert any("used up" in w for w in warnings)


def test_on_render_without_the_store_yelp_is_paused(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("RENDER", "true")
    calls = _fake_yelp(monkeypatch)
    _, n, warnings = _search(["a"])
    assert n == 0 and not calls
    assert any("Yelp is paused because no database is connected yet" in w for w in warnings)


def test_request_json_asks_before_each_retry(monkeypatch):
    attempts, asked = [], []

    class Busy:
        status_code, text, headers = 503, "busy", {}

    monkeypatch.setattr(http.requests, "request", lambda *a, **k: attempts.append(1) or Busy())
    monkeypatch.setattr(http.time, "sleep", lambda s: None)
    with pytest.raises(http.HttpError):
        http.request_json("GET", "https://example.test", use_cache=False, retries=3,
                          before_retry=lambda: asked.append(1) or len(asked) < 2)
    assert len(attempts) == 2 and len(asked) == 2


def test_page_shows_yelp_calls_left(monkeypatch):
    client = web.create_app(password="").test_client()
    assert b'id="yelp-quota"' not in client.get("/").data        # no Yelp key: no line
    monkeypatch.setenv("YELP_API_KEY", YELP_KEY)
    budget = usage.yelp_budget()
    for _ in range(8):
        budget.take()
    page = client.get("/").data.decode()
    assert "Yelp: 42 of 50 calls left in the last 24 hours; all back by " in page
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("RENDER", "true")
    assert "Yelp is paused" in client.get("/").data.decode()


def test_counter_and_cache_survive_in_the_database(monkeypatch):
    calls = _fake_yelp(monkeypatch, total=10)
    _search(["a", "b"])
    assert usage.yelp_budget().used() == 2
    leads, n, _ = _search(["a", "b"])
    assert n == 0 and len(calls) == 2 and len(leads) == 20       # reused, no calls
    assert not list(http.CACHE_DIR.glob("*.json"))               # all in the database


def test_unreachable_database_spends_nothing(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody@127.0.0.1:1/none")
    calls = _fake_yelp(monkeypatch)
    _, n, warnings = _search(["a"])
    assert n == 0 and not calls
    assert any("Yelp is paused because the database could not be reached" in w for w in warnings)
    assert usage.yelp_budget().left() == 0


def test_counter_is_atomic_under_threads():
    import threading
    budget, got = usage.yelp_budget(), []
    threads = [threading.Thread(target=lambda: got.append(budget.take())) for _ in range(80)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert got.count(True) == 50 and budget.used() == 50


def test_calls_come_back_24_hours_after_they_were_made(monkeypatch):
    import datetime as dt
    clock = {"now": dt.datetime(2026, 9, 29, 18, 19, tzinfo=dt.UTC)}   # 12:19 pm in Utah
    monkeypatch.setattr(usage, "_now", lambda: clock["now"])
    budget = usage.yelp_budget()
    assert budget.left() == 50 and budget.reset_text() is None
    for _ in range(3):
        assert budget.take()
    assert budget.left() == 47 and budget.reset_text() == "tomorrow at 12:21 PM"
    clock["now"] = dt.datetime(2026, 9, 30, 6, 0, tzinfo=dt.UTC)     # just past midnight in Utah
    assert budget.left() == 47                     # still counted: not 24 hours yet
    assert budget.reset_text() == "today at 12:21 PM"     # the same time, now on Utah's same day
    for _ in range(47):
        assert budget.take()
    assert not budget.take()
    clock["now"] = dt.datetime(2026, 9, 30, 18, 21, tzinfo=dt.UTC)
    assert budget.left() == 3                      # the first three are back
    clock["now"] = dt.datetime(2026, 10, 1, 7, 0, tzinfo=dt.UTC)
    assert budget.left() == 50


def test_todays_old_count_carries_over(monkeypatch):
    with store.connect() as db:
        db.run("INSERT INTO usage (name, day, used) VALUES ('yelp', ?, 3)",
               (usage._now().strftime("%Y-%m-%d"),))
    assert usage.yelp_budget().left() == 47


def test_page_shows_when_calls_come_back(monkeypatch):
    import datetime as dt
    monkeypatch.setattr(usage, "_now", lambda: dt.datetime(2026, 9, 29, 18, 19, tzinfo=dt.UTC))
    monkeypatch.setenv("YELP_API_KEY", YELP_KEY)
    for _ in range(3):
        usage.yelp_budget().take()
    page = web.create_app(password="").test_client().get("/").data.decode()
    assert "Yelp: 47 of 50 calls left in the last 24 hours; all back by tomorrow at 12:21 PM</div>" in page


def test_small_request_cap_advice_stays_within_the_daily_limit(monkeypatch):
    _fake_yelp(monkeypatch, total=10)
    _, n, warnings = _search([f"a{i}" for i in range(19)], max_requests=10)
    assert n == 10
    assert any("the request cap; up to 50 of the 50 Yelp calls for 24 hours are left" in w for w in warnings)
    assert not any("--max-requests" in w for w in warnings)


def test_unreadable_cache_stops_spending(monkeypatch):
    calls = _fake_yelp(monkeypatch, total=10)
    _search(["a", "b", "c"])
    assert len(calls) == 3
    real = store.open_db

    def flaky():
        raise store.Unavailable("the database could not be reached (OperationalError)")
    monkeypatch.setattr(store, "open_db", flaky)
    monkeypatch.setattr(usage.DailyBudget, "left", lambda self: 47)
    monkeypatch.setattr(usage.DailyBudget, "take", lambda self: True)
    _, n, warnings = _search(["a", "b", "c"])
    assert n == 0 and len(calls) == 3
    assert any("could not be read from the database" in w for w in warnings)
    monkeypatch.setattr(store, "open_db", real)


# ---- the Yelp reset names its day

def test_a_time_names_today_or_tomorrow_in_utah():
    import datetime as dt
    noon = dt.datetime(2026, 9, 29, 18, 0, tzinfo=dt.UTC).timestamp()        # 12:00 PM in Utah
    assert day_clock_text(noon + 3600, noon) == "today at 1:00 PM"
    assert day_clock_text(noon + 24 * 3600, noon) == "tomorrow at 12:00 PM"
    assert day_clock_text(noon + 3 * 24 * 3600, noon) == "Oct 2 at 12:00 PM"
    # 11 PM in Utah is already the next day in UTC: still "today" in Utah.
    late = dt.datetime(2026, 9, 30, 5, 0, tzinfo=dt.UTC).timestamp()
    assert day_clock_text(late, noon) == "today at 11:00 PM"
