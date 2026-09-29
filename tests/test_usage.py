"""The website's hard daily limit on Yelp calls (config.YELP_DAILY_LIMIT)."""

import time

import pytest

from leadgen import config, http, usage, web
from leadgen.sources import yelp

from test_yelp import YELP_KEY, _biz, _fake_yelp


class FakeRedis:
    def __init__(self):
        self.data = {}
        self.down = False

    def _check(self):
        if self.down:
            raise ConnectionError("store unreachable")

    def get(self, key):
        self._check()
        return self.data.get(key)

    def set(self, key, value, nx=False, ex=None):
        self._check()
        if nx and key in self.data:
            return None
        self.data[key] = str(value)
        return True

    def incr(self, key):
        self._check()
        self.data[key] = str(int(self.data.get(key, 0)) + 1)
        return int(self.data[key])

    def decr(self, key):
        self._check()
        self.data[key] = str(int(self.data.get(key, 0)) - 1)
        return int(self.data[key])

    def expire(self, key, seconds):
        self._check()
        return True

    def pipeline(self):
        store, ops = self, []

        class Pipe:
            def incr(self, key):
                ops.append(lambda: store.incr(key))

            def expire(self, key, seconds):
                ops.append(lambda: store.expire(key, seconds))

            def execute(self):
                return [op() for op in ops]
        return Pipe()


@pytest.fixture
def fake_redis(monkeypatch):
    r = FakeRedis()
    monkeypatch.setattr(usage, "redis_client", lambda: r)
    return r


def _store_started_yesterday(r):
    r.data[usage.STORE_SINCE_KEY] = str(time.time() - 2 * 86400)


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
    assert any("Stopped after 20 Yelp calls (this site may make 50 Yelp calls a day and 20 "
               "were left today" in w for w in w2)
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


def test_shared_counter_and_cache_in_redis(fake_redis, monkeypatch):
    _store_started_yesterday(fake_redis)
    calls = _fake_yelp(monkeypatch, total=10)
    _search(["a", "b"])
    assert usage.yelp_budget().used() == 2
    leads, n, _ = _search(["a", "b"])
    assert n == 0 and len(calls) == 2 and len(leads) == 20       # served from Redis
    assert not any(http.CACHE_DIR.glob("*.json"))                 # nothing on local disk
    fake_redis.data[usage.DailyBudget("yelp", 50)._key()] = "50"
    _, n, warnings = _search(["c"])
    assert n == 0 and any("used up" in w for w in warnings)


def test_fresh_store_counts_yelps_own_usage(fake_redis):
    # The store started today (first setup, or Render restarted it): calls made
    # earlier today are unknown, so one call goes out to read Yelp's own count.
    budget = usage.yelp_budget()
    assert budget.take()
    assert not budget.take() and "not reported" in budget.problem
    budget.observe(20)                  # Yelp: 20 calls today, 1 of them counted here
    assert budget.used() == 20 and budget.left() == 30
    for _ in range(30):
        assert budget.take()
    assert not budget.take() and budget.problem is None
    budget.observe(45)                  # only the first report after the restart counts
    assert budget.used() == 50


def test_fresh_store_reads_yelp_headers(fake_redis, monkeypatch):
    calls = []

    def fake(method, url, *, params=None, response_headers=None, **kw):
        calls.append(params)
        response_headers.update({"ratelimit-dailylimit": "300",
                                 "ratelimit-remaining": str(300 - 45 - len(calls))})
        return {"total": 1, "businesses": [_biz(len(calls))]}

    monkeypatch.setattr(yelp, "request_json", fake)
    _, n, warnings = _search([f"q{i}" for i in range(10)])
    # 45 other calls today plus this one: 46 of 50 may be this site's, so 4 more.
    assert n == len(calls) == 5
    assert any("used up" in w for w in warnings)


def test_on_render_without_the_store_yelp_is_paused(monkeypatch):
    monkeypatch.setenv("RENDER", "true")
    calls = _fake_yelp(monkeypatch)
    _, n, warnings = _search(["a"])
    assert n == 0 and not calls
    assert any("Yelp is paused because the daily usage counter is not connected" in w
               for w in warnings)


def test_unreachable_store_spends_nothing(fake_redis, monkeypatch):
    fake_redis.down = True
    calls = _fake_yelp(monkeypatch)
    _, n, warnings = _search(["a"])
    assert n == 0 and not calls
    assert any("Yelp is paused" in w for w in warnings)
    assert usage.yelp_budget().left() == 0


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
    assert "Yelp: 42 of today&#39;s 50 calls left" in page or "Yelp: 42 of today's 50 calls left" in page
    monkeypatch.setenv("RENDER", "true")
    assert "Yelp is paused" in client.get("/").data.decode()
