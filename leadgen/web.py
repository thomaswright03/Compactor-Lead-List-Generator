"""The website: find leads, work the saved list, log calls, see stats, download."""

import datetime as dt
import hashlib
import hmac
import ipaddress
import logging
import math
import os
import re
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import replace
from urllib.parse import urlparse

from flask import Flask, Response, abort, jsonify, redirect, render_template, request, session
from werkzeug.exceptions import HTTPException, ServiceUnavailable

from . import calls, config, daily, localtime, marks, saved, stats, store, usage
from .export import format_phone, to_csv_bytes, to_xlsx_bytes
from .geo import GeocodeError
from .localtime import date_time_text
from .pipeline import GRIDS, SOURCES, PipelineError, SearchParams, run
from .scoring import TIER_LABELS

log = logging.getLogger(__name__)

MAX_JOBS = 20
MAX_KEYWORDS = 20
MAX_KEYWORD_LEN = 60
MAX_REQUESTS_LIMIT = 5000
LOGIN_DAYS = 30                 # how long a login lasts on a device
LOGIN_TRIES = 10                # wrong passwords allowed per address ...
LOGIN_WINDOW = 15 * 60          # ... in this many seconds

# What the pages say when something is down (plain words; the detail goes to the log).
DB_DOWN = "Can't reach your saved leads right now. Nothing is lost. Try again in a minute."
MARKS_DOWN = ("Couldn't load your Yes/No marks and calls right now. Nothing is lost. "
              "Please reload in a minute.")
SEARCH_PAUSED = ("Searching is paused by the administrator. Saved leads, calls and stats "
                 "still work.")
NOT_USED_UP = "Today's search was not used up."
ERROR_PAGES = {
    400: ("Something in that request wasn't right", "Go back and try again."),
    401: ("You're logged out", "Log in again to keep working."),
    403: ("That page isn't available", "It can't be opened from here."),
    404: ("Page not found", "The address may be mistyped, or the page has moved."),
    405: ("That page isn't available", "It can't be opened this way."),
    429: ("Too many tries", "Wait a few minutes and try again."),
    500: ("Something went wrong", "The problem has been logged. Try again in a minute."),
    503: ("Can't reach the saved data right now", "Nothing is lost. Try again in a minute."),
}


def setup_logging():
    """Log to stderr (Render shows it under Logs) unless the host set up logging."""
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(name)s: %(message)s")


class _LoadError(Exception):
    """Saved data could not be read; the message is for the page."""


def _db_message(exc):
    """The page's words for a database that can't be used."""
    if isinstance(exc, store.Unavailable) and "DATABASE_URL" in str(exc):
        return f"Saved leads are off: {exc}."          # a setup problem, for the owner
    return DB_DOWN


def load_saved():
    """Every saved lead with its mark and latest call, plus the undo offers, on one
    database connection. Raises _LoadError with a plain message when any of it can't
    be read: leads shown without their marks would all look unchecked."""
    try:
        leads = saved.load(with_marks=False)
    except Exception as exc:
        log.warning("Loading the saved leads failed", exc_info=True)
        raise _LoadError(_db_message(exc)) from exc
    try:
        marks.apply(leads)
        calls.apply(leads)
        undo = {"mark": marks.pending_undos(), "call": calls.pending_undos()}
    except Exception as exc:
        log.warning("Loading the marks or calls failed", exc_info=True)
        raise _LoadError(MARKS_DOWN) from exc
    return leads, undo


_TECHNICAL = re.compile(r"https?://|HTTP \d{3}|Error\b|Exception|<html|\(\w+Error", re.I)
_SOURCE_WORDS = (("Google", "Google"), ("Yelp", "Yelp"), ("OpenStreetMap", "The map data service"),
                 ("Overpass", "The map data service"))


def plain_warning(text):
    """A search note in words for sales staff: no URLs, error names or cap settings.
    The original goes to the log."""
    who = next((name for word, name in _SOURCE_WORDS if word.lower() in text.lower()), "")
    if _TECHNICAL.search(text):
        log.info("Search note: %s", text)
        return (f"{who or 'One of the sources'} had a problem with part of the search; "
                "the businesses already found were kept.")
    if "request cap" in text or "--max-requests" in text:
        log.info("Search note: %s", text)
        return (f"{who or 'A paid source'} stopped at the limit on paid lookups for one "
                "search, so some searches were skipped.")
    if "No Google Places or Yelp API key" in text:
        return ("Google and Yelp aren't set up, so only the free map data was searched "
                "(it has fewer phone numbers).")
    return text


def _lead_json(lead, undo=None):
    undo = undo or {}
    return {
        "score": lead.score, "tier": lead.tier, "tier_label": TIER_LABELS.get(lead.tier, ""),
        "lead_type": lead.lead_type, "flags": lead.flags, "name": lead.name,
        "category": lead.category, "address": lead.address, "city": lead.city,
        "zip": lead.zip, "phone": format_phone(lead.phone), "website": lead.website,
        "distance": lead.distance_miles, "reasons": lead.reasons, "map_url": lead.map_url,
        "sources": lead.sources, "key": lead.uid, "has_baler": lead.has_baler,
        "call_outcome": lead.call_outcome, "call_notes": lead.call_notes,
        "call_count": lead.call_count, "last_call": date_time_text(lead.last_call_at),
        "last_call_at": lead.last_call_at,
        "undo_mark": undo.get("mark", {}).get(lead.uid),
        "undo_call": undo.get("call", {}).get(lead.uid),
    }


STEPS = ["Find the location", "Search Yelp and Google", "Search map data", "Merge and score",
         "Save"]
# A step running longer than this (seconds) is "taking longer than usual" on the page.
SLOW_SECONDS = [30, 240, 75, 60, 60]


class _Progress:
    """Turns the search's progress messages into a step and a percentage for the page.

    The Yelp / Google part advances with each call; the map data comes back in one
    slow request, so its share of the bar fills gradually while it runs.
    """

    def __init__(self, job):
        self.job = job
        self.calls, self.cap, self.map_started = 0, None, None
        self.step_started = time.time()
        job.update(step=0, pct=1.0, started=time.time())

    def slow(self):
        step = self.job["step"]
        return step < len(SLOW_SECONDS) and time.time() - self.step_started > SLOW_SECONDS[step]

    def __call__(self, msg):
        job = self.job
        job["message"] = msg
        if msg.startswith("Locating"):
            self._at(0, 3)
        elif re.match(r"(Yelp|Google): ", msg):
            cap = re.search(r"up to (\d+)", msg)
            self.cap = (self.cap or 0) + (int(cap.group(1)) if cap else 0)
            self._at(1, 6)
        elif re.match(r"(Yelp|Google) page \d", msg):
            self.calls += 1
            self._at(1, 6 + 42 * min(1.0, self.calls / max(self.cap or 1, 1)))
        elif msg.startswith("OpenStreetMap"):
            self.map_started = self.map_started or time.time()
            self._at(2, 50)
        elif msg.startswith("Filtering"):
            self._at(3, 88)
        elif msg.startswith("Merging"):
            self._at(3, 92)
        elif msg.startswith("Saving"):
            self._at(4, 96)

    def _at(self, step, pct):
        if step > self.job["step"]:
            self.step_started = time.time()
        self.job["step"] = max(self.job["step"], step)
        self.job["pct"] = max(self.job["pct"], pct)

    def pct(self):
        if self.job["step"] == 2 and self.map_started:
            # Creeps toward 86% over the few minutes the map servers take.
            return max(self.job["pct"],
                       50 + 36 * (1 - math.exp(-(time.time() - self.map_started) / 150)))
        return self.job["pct"]


def skipped_steps(params):
    """Steps a search won't run: Yelp and Google when neither is set up (or both are
    switched off), map data when a paid source alone was picked."""
    google, yelp_key, _ = params.resolved_keys()
    paid = (params.source in ("google", "both", "yelp")
            or (params.source == "auto" and (google or yelp_key)))
    return ([] if paid else [1]) + ([] if params.source in ("osm", "both", "auto") else [2])


def switches():
    """The administrator's off switches that are set right now (see config)."""
    return {"search_paused": config.switched_on(config.SEARCH_PAUSED_ENV),
            "google_off": config.switched_on(config.GOOGLE_OFF_ENV),
            "yelp_off": config.switched_on(config.YELP_OFF_ENV)}


def yelp_quota():
    """Today's Yelp calls left for this site, or None when no Yelp key is set."""
    if not SearchParams().resolved_keys()[1]:
        return None
    budget = usage.yelp_budget()
    left = budget.left()
    if budget.problem:
        text = f"Yelp is paused: {budget.problem}."
    else:
        reset = budget.reset_text()
        text = (f"Yelp: {left} of today's {budget.limit} calls left"
                + (f" (resets at {reset})" if reset else ""))
    return {"left": left, "limit": budget.limit, "paused": bool(budget.problem), "text": text}


def create_app(password=None, username=None):
    """password (or the APP_PASSWORD env var) puts the whole site behind a login page.

    username (or APP_USERNAME) is the name to log in with; without one any name
    works. Always set a password when the page is reachable from the internet:
    every search can spend the Google or Yelp API key.
    """
    setup_logging()
    app = Flask(__name__)
    password = password if password is not None else os.environ.get("APP_PASSWORD", "")
    username = (username if username is not None else os.environ.get("APP_USERNAME", "")).strip()
    # Logins are kept in a signed cookie. SECRET_KEY can be set; otherwise the key is
    # derived from the login itself, so changing the password logs everyone out.
    app.secret_key = os.environ.get("SECRET_KEY") or hmac.new(
        f"{username}\n{password}".encode(), b"lead-finder-session-v1", hashlib.sha256).digest()
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                      SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER")),
                      PERMANENT_SESSION_LIFETIME=dt.timedelta(days=LOGIN_DAYS))
    failures = {}                  # address -> times of recent wrong passwords
    jobs = OrderedDict()

    allowed_hosts = {"localhost"} | {
        h.strip().lower() for h in os.environ.get("LEADGEN_ALLOWED_HOSTS", "").split(",")
        if h.strip()}

    def _host_allowed():
        """Block DNS rebinding: a foreign site's name that now points at this server."""
        host = request.host.lower()
        name = host[1:host.index("]")] if host.startswith("[") else host.rsplit(":", 1)[0]
        try:
            ipaddress.ip_address(name)
            return True           # an IP literal cannot be rebound
        except ValueError:
            return name in allowed_hosts

    def _wants_page():
        """A browser opening a page (not the page's own data requests)."""
        accept = request.accept_mimetypes
        return request.method == "GET" and accept["text/html"] > accept["application/json"]

    @app.context_processor
    def page_globals():
        return {"year": localtime.now().year, "login_on": bool(password),
                "user": session.get("user", "")}

    # One database connection per request, shared by everything the request reads.
    @app.before_request
    def open_scope():
        store.begin_scope()

    @app.teardown_request
    def close_scope(exc):
        store.end_scope()

    @app.before_request
    def require_login():
        if request.path == "/healthz":
            return None           # holds no data; lets uptime checks see the deployed version
        if not password:
            # Without a login only local/IP access is allowed (see LEADGEN_ALLOWED_HOSTS).
            return None if _host_allowed() else abort(403)
        if request.path == "/login":
            return None
        if session.get("login") == _login_token():
            return None
        if request.method == "GET" and (_wants_page() or request.path == "/"
                                        or request.path.startswith("/download/")):
            return redirect("/login")
        return jsonify({"error": "You were logged out. Reload the page to log in again."}), 401

    def _error_page(code, title=None, text=None):
        default_title, default_text = ERROR_PAGES.get(code, ERROR_PAGES[500])
        return render_template("error.html", code=code, title=title or default_title,
                               text=text or default_text), code

    @app.errorhandler(HTTPException)
    def http_error(exc):
        code = exc.code or 500
        if _wants_page() or request.path.startswith("/download/"):
            text = exc.description if getattr(exc, "plain", False) else None
            return _error_page(code, text=text)
        title = ERROR_PAGES.get(code, ERROR_PAGES[500])[0]
        return jsonify({"error": title}), code

    @app.errorhandler(Exception)
    def unexpected_error(exc):
        log.exception("Unexpected error on %s %s", request.method, request.path)
        if _wants_page() or request.path.startswith("/download/"):
            return _error_page(500)
        return jsonify({"error": "Something went wrong. Try again in a minute."}), 500

    def _unavailable(text):
        """A 503 whose message the error page shows as it is."""
        exc = ServiceUnavailable(text)
        exc.plain = True
        raise exc

    def _login_token():
        """Changes when the username or password changes, which ends existing logins."""
        return hmac.new(app.secret_key if isinstance(app.secret_key, bytes)
                        else app.secret_key.encode(), f"{username}\n{password}".encode(),
                        hashlib.sha256).hexdigest()[:32]

    def _client_address():
        # On Render the last X-Forwarded-For entry is the one its proxy added (the visitor).
        forwarded = request.headers.get("X-Forwarded-For", "")
        if os.environ.get("RENDER") and forwarded:
            return forwarded.split(",")[-1].strip()
        return request.remote_addr or ""

    def _login_page(error="", name="", status=200):
        return render_template("login.html", error=error, username=name,
                               contact=os.environ.get("LEADGEN_SUPPORT_CONTACT", "").strip()
                               ), status

    @app.get("/login")
    def login_page():
        if not password or session.get("login") == _login_token():
            return redirect("/")
        return _login_page()

    @app.post("/login")
    def login():
        if not password:
            return redirect("/")
        if not _same_origin():
            abort(403)
        now, who = time.time(), _client_address()
        recent = [t for t in failures.get(who, []) if now - t < LOGIN_WINDOW]
        name = (request.form.get("username") or "").strip()
        if len(recent) >= LOGIN_TRIES:
            return _login_page("Too many wrong tries. Wait 15 minutes and try again.", name, 429)
        supplied = request.form.get("password") or ""
        name_ok = not username or hmac.compare_digest(name.encode(), username.encode())
        if name_ok and hmac.compare_digest(supplied.encode(), password.encode()):
            failures.pop(who, None)
            session.clear()
            session.permanent = True
            session.update(login=_login_token(), user=username or name)
            return redirect("/")
        failures[who] = [*recent, now]
        if len(failures) > 5000:              # keep memory bounded
            failures.clear()
        time.sleep(0.5)
        return _login_page("Wrong username or password.", name, 401)

    @app.post("/logout")
    def logout():
        if not _same_origin():
            abort(403)
        session.clear()
        return redirect("/login")

    lock = threading.Lock()

    def _parse_form(form):
        """Validate the form strictly; raise ValueError with a message for the page."""
        def number(name, default, cast, lo, hi, label):
            raw = (form.get(name) or "").strip()
            if not raw:
                return default
            try:
                value = float(raw)
            except ValueError:
                raise ValueError(f"{label} must be a number") from None
            if cast is int:
                if not math.isfinite(value) or not value.is_integer():
                    raise ValueError(f"{label} must be a whole number")
                value = int(value)
            if not lo <= value <= hi:
                raise ValueError(f"{label} must be between {lo} and {hi}")
            return value

        keywords = [k.strip() for k in form.get("keywords", "").split(",") if k.strip()]
        if len(keywords) > MAX_KEYWORDS or any(len(k) > MAX_KEYWORD_LEN for k in keywords):
            raise ValueError(f"Use at most {MAX_KEYWORDS} keywords of up to "
                             f"{MAX_KEYWORD_LEN} characters")
        source = form.get("source", "auto")
        if source not in SOURCES:
            raise ValueError("Pick where to search from the list")
        grid = number("grid", 1, int, 1, 19, "Coverage")
        if grid not in GRIDS:
            raise ValueError("Pick a coverage from the list")
        return SearchParams(
            location=(form.get("location", "").strip() or config.DEFAULT_LOCATION)[:200],
            radius_miles=number("radius", config.DEFAULT_RADIUS_MILES, float, 1, 100,
                                "Radius"),
            keywords=keywords,
            source=source,
            min_score=number("min_score", config.DEFAULT_MIN_SCORE, int, 0, 100,
                             "Minimum score"),
            grid=grid,
            max_requests=number("max_requests", None, int, 1, MAX_REQUESTS_LIMIT,
                                "The limit on paid lookups"),
            only_keyword_matches=form.get("only_keyword_matches") == "on",
        )

    def _worker(job, params, day):
        progress = job["progress"]
        try:
            # Closed places come back too, so a saved one that has since closed is updated
            # (and leaves the saved list); they are not shown.
            result = run(replace(params, include_closed=True), progress)
            for problem in result.problems:
                log.warning("Search %s: a source failed: %s", day, problem)
            warnings = [plain_warning(w) for w in result.warnings]
            try:
                progress("Saving leads")
                job["new_leads"], _ = saved.save_search(result.leads, params.keywords)
                job["saved"] = True
            except Exception as exc:
                log.exception("Saving the search's leads failed")
                why = _db_message(exc) if isinstance(exc, store.Unavailable) else (
                    "the database had a problem")
                warnings.append(f"These leads were not saved: {why}")
            if not params.include_closed:
                result.leads = [lead for lead in result.leads
                                if lead.business_status != "CLOSED_PERMANENTLY"]
            result.warnings = warnings
            # The day's record is written before the job says it is done, so the page's
            # search history is up to date when it reloads it.
            try:
                daily.finish(day, {"leads": len(result.leads), "new": job.get("new_leads"),
                                   "found_near": result.location_label,
                                   "details": result.stats, "warnings": warnings})
            except Exception:
                log.exception("Recording today's search failed; retrying with the count only")
                # Try once more with just the count: without it the day would look unfinished
                # and free up again after daily.STALE_SECONDS.
                try:
                    daily.finish(day, {"leads": len(result.leads)})
                except Exception:
                    log.exception("Recording today's search failed again")
            job.update(state="done", result=result, message="Done", pct=100, step=len(STEPS))
        except GeocodeError as exc:
            log.info("Search %s: location not found: %s", day, exc)
            _fail(job, day, f"{exc} {NOT_USED_UP}", str(exc))
        except PipelineError as exc:
            log.warning("Search %s failed: %s %s", day, exc, exc.detail)
            _fail(job, day, f"{exc} {NOT_USED_UP} Try again in an hour.", str(exc))
        except Exception:  # show unexpected failures instead of spinning forever
            log.exception("Search %s failed unexpectedly", day)
            _fail(job, day, f"Something went wrong during the search. {NOT_USED_UP} Try again "
                            "in an hour; if it keeps happening, tell whoever looks after the "
                            "site.", "Something went wrong during the search.")

    def _fail(job, day, message, reason):
        _give_back(day, reason)
        job.update(state="error", message=message)

    def _give_back(day, reason=None):
        """A search that failed outright does not use up the day (it stays in the
        history as failed, with the reason)."""
        try:
            daily.release(day, reason)
        except Exception:
            log.exception("Giving back today's search failed")

    def _same_origin():
        """Stop other websites from starting (billed) searches through this server."""
        site = request.headers.get("Sec-Fetch-Site")
        if site not in (None, "same-origin", "none"):
            return False
        origin = request.headers.get("Origin") or request.headers.get("Referer")
        return not origin or urlparse(origin).netloc == request.host

    @app.get("/healthz")
    def healthz():
        return jsonify({"ok": True, "version": os.environ.get("RENDER_GIT_COMMIT", "")[:7]})

    @app.get("/")
    def index():
        google, yelp_key, _ = SearchParams().resolved_keys()
        return render_template("index.html", defaults={
            "location": config.DEFAULT_LOCATION, "radius": config.DEFAULT_RADIUS_MILES,
            "keywords": ", ".join(config.DEFAULT_KEYWORDS), "min_score": config.DEFAULT_MIN_SCORE,
        }, yelp=yelp_quota(), outcomes=list(calls.OUTCOMES), google_on=bool(google),
            yelp_on=bool(yelp_key), switches=switches(), paused_text=SEARCH_PAUSED,
            undo_seconds=marks.UNDO_SECONDS)

    @app.post("/search")
    def search():
        if not _same_origin():
            abort(403)
        if config.switched_on(config.SEARCH_PAUSED_ENV):
            return jsonify({"error": SEARCH_PAUSED, "paused": True}), 503
        try:
            params = _parse_form(request.form)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        job_id = uuid.uuid4().hex[:12]
        job = {"state": "running", "message": "Starting", "params": params,
               "skipped": skipped_steps(params)}
        job["progress"] = _Progress(job)
        with lock:
            running = next((k for k, j in jobs.items() if j["state"] == "running"), None)
        if running is not None:
            # The page attaches to it (e.g. after a reload) instead of starting another.
            return jsonify({"error": "A search is already running.", "job_id": running}), 429
        try:
            day, done = daily.claim({"location": params.location, "radius": params.radius_miles,
                                     "keywords": ", ".join(params.keywords),
                                     "source": params.source})
        except Exception as exc:
            log.warning("Recording the search failed", exc_info=True)
            if isinstance(exc, store.Unavailable) and "DATABASE_URL" in str(exc):
                return jsonify({"error": f"Searching needs the database: {exc}."}), 503
            return jsonify({"error": "Can't reach the saved data right now, so the search "
                                     "didn't start. Nothing is lost. Try again in a minute."}), 503
        if day is None:
            return jsonify({"error": f"Today's search was already run ({done['when']}). "
                                     "Find Leads works once a day; the next search can run "
                                     "tomorrow.", "searched": done}), 409
        with lock:
            jobs[job_id] = job
            # Evict the oldest finished jobs; never a running one.
            while len(jobs) > MAX_JOBS:
                old = next((k for k, v in jobs.items() if v["state"] != "running"), None)
                if old is None:
                    break
                del jobs[old]
        try:
            threading.Thread(target=_worker, args=(job, params, day), daemon=True).start()
        except RuntimeError:
            log.exception("Starting the search thread failed")
            with lock:
                jobs.pop(job_id, None)
            _give_back(day)
            return jsonify({"error": "Could not start the search. Try again."}), 503
        return jsonify({"job_id": job_id})

    @app.get("/leads")
    def saved_leads():
        """Every saved lead, for the page to show before any search."""
        try:
            leads, undo = load_saved()
        except _LoadError as exc:
            return jsonify({"error": str(exc)}), 503
        return jsonify({"leads": [_lead_json(lead, undo) for lead in leads], "now": time.time()})

    @app.post("/calls")
    def log_call():
        """Record a call to a lead: its outcome and the conversation notes (kept for good)."""
        if not _same_origin():
            abort(403)
        data = request.get_json(silent=True) or {}
        uid = str(data.get("key") or "")
        if not uid or len(uid) > 64:
            return jsonify({"error": "Unknown lead"}), 400
        try:
            call = calls.log_call(uid, str(data.get("outcome") or ""), str(data.get("notes") or ""))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            log.warning("Saving a call failed", exc_info=True)
            return jsonify({"error": f"The call wasn't saved. {_db_message(exc)}"}), 503
        return jsonify({"ok": True, "now": time.time(),
                        "call": {**call, "when": date_time_text(call["at"]),
                                 "undo": {"id": call["id"],
                                          "until": call["at"] + calls.UNDO_SECONDS}}})

    @app.get("/calls/<uid>")
    def call_history(uid):
        try:
            return jsonify({"calls": calls.history(uid[:64])})
        except Exception as exc:
            log.warning("Loading a call history failed", exc_info=True)
            return jsonify({"error": f"Couldn't load the calls. {_db_message(exc)}"}), 503

    @app.get("/stats")
    def stats_data():
        try:
            leads, _ = load_saved()
        except _LoadError as exc:
            return jsonify({"error": str(exc)}), 503
        return jsonify({**stats.summarize(leads), "min_score": config.DEFAULT_MIN_SCORE})

    @app.get("/searches")
    def searches():
        """Each day's search (when, what, what it found), and whether today's is used."""
        try:
            body = daily.history()
        except Exception as exc:
            log.warning("Loading the search history failed", exc_info=True)
            return jsonify({"error": _db_message(exc)}), 503
        with lock:
            running = next((k for k, j in jobs.items() if j["state"] == "running"), None)
        return jsonify({**body, "yelp": yelp_quota(), "running": running,
                        "paused": SEARCH_PAUSED if switches()["search_paused"] else None})

    @app.post("/mark")
    def mark():
        """Save whether a business has a baler ("yes" or "no"). A mark is kept for good:
        it can be switched but not cleared (except by undoing a click within
        marks.UNDO_SECONDS), and later searches keep it with the business."""
        if not _same_origin():
            abort(403)
        data = request.get_json(silent=True) or {}
        uid, value = str(data.get("key") or ""), str(data.get("value") or "")
        if not uid or len(uid) > 64 or value not in marks.VALUES:
            return jsonify({"error": "Bad mark"}), 400
        try:
            undo = marks.set_mark(uid, value)
        except ValueError:
            return jsonify({"error": "That business isn't in the saved list. Reload the "
                                     "page."}), 404
        except Exception as exc:
            log.warning("Saving a mark failed", exc_info=True)
            return jsonify({"error": f"The answer wasn't saved. {_db_message(exc)}"}), 503
        return jsonify({"ok": True, "undo": undo, "now": time.time()})

    def _undo(fn, what):
        if not _same_origin():
            abort(403)
        change = str((request.get_json(silent=True) or {}).get("id") or "")
        if not change or len(change) > 64:
            return jsonify({"error": "Bad undo"}), 400
        try:
            ok = fn(change)
        except Exception as exc:
            log.warning("Undoing a %s failed", what, exc_info=True)
            return jsonify({"error": f"Couldn't undo. {_db_message(exc)}"}), 503
        if not ok:
            return jsonify({"error": "It's too late to undo that (after 5 minutes, or once "
                                     "there's a newer change)."}), 409
        return jsonify({"ok": True})

    @app.post("/mark/undo")
    def undo_mark():
        """Undo a Yes/No click made in the last few minutes (a misclick), by its id."""
        return _undo(marks.undo, "mark")

    @app.post("/calls/undo")
    def undo_call():
        """Undo a call saved in the last few minutes (a misclick)."""
        return _undo(calls.undo, "call")

    @app.get("/status/<job_id>")
    def status(job_id):
        job = jobs.get(job_id) or abort(404)
        progress = job["progress"]
        body = {"state": job["state"], "message": job["message"], "steps": STEPS,
                "step": job.get("step", 0), "pct": round(progress.pct(), 1),
                "elapsed": round(time.time() - job.get("started", time.time())),
                "skipped": job.get("skipped", []),
                "slow": job["state"] == "running" and progress.slow()}
        if job["state"] == "done":
            res = job["result"]
            leads, stats, undo = res.leads, dict(res.stats), {}
            warnings, shows_saved = list(res.warnings), False
            if job.get("saved"):
                try:
                    leads, undo = load_saved()
                    stats.update({"new leads saved": job["new_leads"], "saved leads": len(leads)})
                    shows_saved = True
                except _LoadError as exc:
                    warnings.append(str(exc))
            body.update(leads=[_lead_json(lead, undo) for lead in leads], stats=stats,
                        warnings=warnings, location=res.location_label,
                        yelp=yelp_quota(), saved=shows_saved)
        return jsonify(body)

    @app.get("/download/<job_id>.<fmt>")
    def download(job_id, fmt):
        """A search's leads, or with job id "saved" every saved lead."""
        if fmt not in ("csv", "xlsx"):
            abort(404)
        if job_id == "saved":
            try:
                leads, _ = load_saved()
            except _LoadError as exc:
                _unavailable(str(exc))
            info = {"list": "All saved leads", "leads": len(leads)}
        else:
            job = jobs.get(job_id)
            if not job or job["state"] != "done":
                abort(404)
            try:
                leads = calls.apply(marks.apply(job["result"].leads))
            except Exception:
                log.warning("Loading marks for a download failed", exc_info=True)
                _unavailable(MARKS_DOWN)
            info = job["result"].run_info(job["params"])
        if fmt == "csv":
            return Response(to_csv_bytes(leads), mimetype="text/csv",
                            headers={"Content-Disposition": "attachment; filename=compactor-leads.csv"})
        data = to_xlsx_bytes(leads, info)
        return Response(data, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": "attachment; filename=compactor-leads.xlsx"})

    return app


if __name__ == "__main__":
    from .envfile import load_dotenv
    load_dotenv()
    create_app().run(debug=True)
