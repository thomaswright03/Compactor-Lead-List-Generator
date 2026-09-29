"""Daily API call budgets and a small cache that outlive restarts.

On Render's free plan the web service's disk and memory are wiped whenever it
sleeps or redeploys, so a count kept there could reset mid-day. The count (and
Yelp's cached results) therefore live in Render Key Value (Redis), found
through REDIS_URL. Elsewhere, for the command line, a small file in the cache
folder is used. On Render without REDIS_URL the budget counts as used up: a
count that could reset mid-day cannot promise the limit.

A free Key Value store loses its data if Render restarts it. So on a day the
store started fresh (its first day, or after a restart), calls made before it
started are unknown, and the budget also counts every call the API itself
reports for that day (Yelp's RateLimit headers), which includes them. Until
the API has reported, only one call is let through to get that report.
"""

import datetime as dt
import hashlib
import json
import os
import threading
import time

from . import config, http

STORE_SINCE_KEY = "leadgen:store-since"
KEEP_SECONDS = 2 * 86400

_lock = threading.Lock()
_client = {}


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _today():
    return _now().strftime("%Y-%m-%d")


def _day_start():
    return _now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def redis_client():
    """The shared Redis connection, or None when REDIS_URL is not set."""
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        return None
    if url not in _client:
        import redis
        _client.clear()
        _client[url] = redis.Redis.from_url(url, socket_timeout=5, socket_connect_timeout=5,
                                            decode_responses=True)
    return _client[url]


def on_render():
    return bool(os.environ.get("RENDER"))


def reset_time_text():
    """When a UTC-day budget resets, in Utah time: '6 pm Utah time'."""
    midnight = (_now() + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0,
                                                       microsecond=0)
    try:
        from zoneinfo import ZoneInfo
        local = midnight.astimezone(ZoneInfo("America/Denver"))
    except Exception:
        return "midnight UTC"
    hour = local.hour % 12 or 12
    return f"{hour} {'am' if local.hour < 12 else 'pm'} Utah time"


class DailyBudget:
    """At most `limit` calls per UTC day, counted across every search and restart."""

    def __init__(self, name, limit):
        self.name = name
        self.limit = limit
        self.problem = None          # why the budget cannot be used right now

    def _key(self):
        return f"leadgen:budget:{self.name}:{_today()}"

    def _before_key(self):
        return self._key() + ":before-store"

    def _file(self):
        return http.CACHE_DIR / f"{self.name}-usage.json"

    def _read_file(self):
        try:
            data = json.loads(self._file().read_text())
            return int(data["used"]) if data.get("day") == _today() else 0
        except (OSError, ValueError, KeyError, TypeError):
            return 0

    def _write_file(self, used):
        try:
            http.CACHE_DIR.mkdir(parents=True, exist_ok=True)
            tmp = self._file().with_suffix(".tmp")
            tmp.write_text(json.dumps({"day": _today(), "used": used}))
            tmp.replace(self._file())
        except OSError:
            pass

    def _unavailable(self, why):
        self.problem = why
        return None

    def _redis(self):
        r = redis_client()
        if r is None and on_render():
            return self._unavailable("the daily usage counter is not connected (REDIS_URL is "
                                     "not set; sync the Blueprint in Render)")
        return r

    def _fresh(self, r):
        """(store started today, calls it cannot know about or None while unknown)."""
        since = r.get(STORE_SINCE_KEY)
        if since is None:
            r.set(STORE_SINCE_KEY, str(time.time()), nx=True)
            since = r.get(STORE_SINCE_KEY)
        if float(since or 0) < _day_start():
            return False, 0
        before = r.get(self._before_key())
        return True, (int(before) if before is not None else None)

    def used(self):
        """Calls spent today; the full limit when the count cannot be read."""
        self.problem = None
        r = self._redis()
        if self.problem:
            return self.limit
        if r is None:
            with _lock:
                return self._read_file()
        try:
            _, before = self._fresh(r)
            return min(self.limit, int(r.get(self._key()) or 0) + (before or 0))
        except Exception as exc:       # unreachable counter: spend nothing
            self._unavailable(f"the daily usage counter could not be read ({exc.__class__.__name__})")
            return self.limit

    def left(self):
        return max(0, self.limit - self.used())

    def take(self):
        """Reserve one call. False when today's calls are used up or cannot be counted."""
        self.problem = None
        r = self._redis()
        if self.problem:
            return False
        if r is None:
            with _lock:
                used = self._read_file()
                if used >= self.limit:
                    return False
                self._write_file(used + 1)
                return True
        key = self._key()
        try:
            fresh, before = self._fresh(r)
            pipe = r.pipeline()
            pipe.incr(key)
            pipe.expire(key, KEEP_SECONDS)
            count = pipe.execute()[0]
            if fresh and before is None:
                if count == 1:
                    return True         # the one call that gets the API's own count
                r.decr(key)
                self.problem = ("the usage counter started fresh today and Yelp has not "
                                "reported today's usage yet, so it cannot be sure of the limit")
                return False
            if count + before > self.limit:
                r.decr(key)
                return False
            return True
        except Exception as exc:
            self._unavailable(f"the daily usage counter could not be updated ({exc.__class__.__name__})")
            return False

    def observe(self, api_used_today):
        """Record the API's own count of today's calls (all users of the key).

        Only matters on a day the store started fresh: every call this site made
        before then is inside that count, so the part not counted here yet bounds them.
        """
        try:
            r = redis_client()
            if r is None:
                return
            fresh, before = self._fresh(r)
            if not fresh or before is not None:
                return
            counted = int(r.get(self._key()) or 0)
            r.set(self._before_key(), max(0, int(api_used_today) - counted), nx=True,
                  ex=KEEP_SECONDS)
        except Exception:
            pass                        # take() then keeps waiting for a report


def yelp_budget():
    return DailyBudget("yelp", config.YELP_DAILY_LIMIT)


class SharedCache:
    """Cache that uses Redis when it is set up (it survives restarts), else local files."""

    def get(self, key, ttl=None):
        r = redis_client()
        if r is None:
            return http.cache_get(key, ttl)
        try:
            raw = r.get(self._key(key))
            return json.loads(raw) if raw else None
        except Exception:
            return None

    def put(self, key, value, ttl=None):
        r = redis_client()
        if r is None:
            return http.cache_put(key, value)
        try:
            r.set(self._key(key), json.dumps(value, separators=(",", ":")),
                  ex=int(ttl or config.CACHE_TTL_SECONDS))
        except Exception:
            pass                        # a full or unreachable store only costs a cache miss

    @staticmethod
    def _key(key):
        return "leadgen:cache:" + hashlib.sha256(key.encode()).hexdigest()
