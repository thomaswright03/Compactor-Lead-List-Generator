"""Minimal web page: fill in the form, watch progress, preview, download CSV/Excel."""

import datetime as dt
import hashlib
import hmac
import ipaddress
import math
import re
import time
import os
import threading
import uuid
from collections import OrderedDict
from dataclasses import replace
from urllib.parse import urlparse

from flask import (Flask, Response, abort, jsonify, redirect, render_template, request,
                   session)

from . import calls, config, daily, marks, saved, stats, store, usage
from .export import format_phone, to_csv_bytes, to_xlsx_bytes
from .geo import GeocodeError
from .http import redact
from .pipeline import GRIDS, SOURCES, PipelineError, SearchParams, run
from .scoring import TIER_LABELS

MAX_JOBS = 20
MAX_KEYWORDS = 20
MAX_KEYWORD_LEN = 60
MAX_REQUESTS_LIMIT = 5000
LOGIN_DAYS = 30                 # how long a login lasts on a device
LOGIN_TRIES = 10                # wrong passwords allowed per address ...
LOGIN_WINDOW = 15 * 60          # ... in this many seconds


def _lead_json(lead):
    return {
        "score": lead.score, "tier": lead.tier, "tier_label": TIER_LABELS.get(lead.tier, ""),
        "lead_type": lead.lead_type, "flags": lead.flags, "name": lead.name,
        "category": lead.category, "address": lead.address, "city": lead.city,
        "zip": lead.zip, "phone": format_phone(lead.phone), "website": lead.website,
        "distance": lead.distance_miles, "reasons": lead.reasons, "map_url": lead.map_url,
        "sources": lead.sources, "key": lead.uid, "has_baler": lead.has_baler,
        "call_outcome": lead.call_outcome, "call_notes": lead.call_notes,
        "call_count": lead.call_count, "last_call": calls.local_time_text(lead.last_call_at),
        "last_call_at": lead.last_call_at,
    }


STEPS = ["Find the location", "Search Yelp and Google", "Search map data", "Merge and score",
         "Save"]


class _Progress:
    """Turns the search's progress messages into a step and a percentage for the page.

    The Yelp / Google part advances with each call; the map data comes back in one
    slow request, so its share of the bar fills gradually while it runs.
    """

    def __init__(self, job):
        self.job = job
        self.calls, self.cap, self.map_started = 0, None, None
        job.update(step=0, pct=1.0, started=time.time())

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
        self.job["step"] = max(self.job["step"], step)
        self.job["pct"] = max(self.job["pct"], pct)

    def pct(self):
        if self.job["step"] == 2 and self.map_started:
            # Creeps toward 86% over the few minutes the map servers take.
            return max(self.job["pct"],
                       50 + 36 * (1 - math.exp(-(time.time() - self.map_started) / 150)))
        return self.job["pct"]


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

    @app.context_processor
    def page_globals():
        return {"year": dt.date.today().year, "login_on": bool(password),
                "user": session.get("user", "")}

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
        if request.method == "GET" and (request.path == "/" or request.path.startswith("/download/")):
            return redirect("/login")
        return jsonify({"error": "You were logged out. Reload the page to log in again."}), 401

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

    @app.get("/login")
    def login_page():
        if not password or session.get("login") == _login_token():
            return redirect("/")
        return render_template("login.html", error="")

    @app.post("/login")
    def login():
        if not password:
            return redirect("/")
        if not _same_origin():
            abort(403)
        now, who = time.time(), _client_address()
        recent = [t for t in failures.get(who, []) if now - t < LOGIN_WINDOW]
        if len(recent) >= LOGIN_TRIES:
            return render_template("login.html", error="Too many wrong tries. Wait 15 minutes "
                                   "and try again."), 429
        name = (request.form.get("username") or "").strip()
        supplied = request.form.get("password") or ""
        name_ok = not username or hmac.compare_digest(name.lower().encode(),
                                                      username.lower().encode())
        if name_ok and hmac.compare_digest(supplied.encode(), password.encode()):
            failures.pop(who, None)
            session.clear()
            session.permanent = True
            session.update(login=_login_token(), user=username or name)
            return redirect("/")
        failures[who] = recent + [now]
        if len(failures) > 5000:              # keep memory bounded
            failures.clear()
        time.sleep(0.5)
        return render_template("login.html", error="Wrong username or password.",
                               username=name), 401

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
                raise ValueError(f"{label} must be a number")
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
            raise ValueError("Unknown data source")
        grid = number("grid", 1, int, 1, 19, "Coverage")
        if grid not in GRIDS:
            raise ValueError("Coverage must be 1, 7 or 19 areas")
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
                                "Request cap"),
            only_keyword_matches=form.get("only_keyword_matches") == "on",
        )

    def _worker(job, params, day):
        progress = job["progress"]
        try:
            # Closed places come back too, so a saved one that has since closed is updated
            # (and leaves the saved list); they are not shown.
            result = run(replace(params, include_closed=True), progress)
            try:
                progress("Saving leads")
                job["new_leads"], _ = saved.save_search(result.leads, params.keywords)
                job["saved"] = True
            except Exception as exc:     # the search still shows; it just is not kept
                why = str(exc) if isinstance(exc, store.Unavailable) else redact(exc)
                result.warnings.append(f"These leads were not saved: {why}.")
            if not params.include_closed:
                result.leads = [l for l in result.leads
                                if l.business_status != "CLOSED_PERMANENTLY"]
            # The day's record is written before the job says it is done, so the page's
            # search history is up to date when it reloads it.
            try:
                daily.finish(day, {"leads": len(result.leads), "new": job.get("new_leads"),
                                   "found_near": result.location_label,
                                   "details": result.stats, "warnings": result.warnings})
            except Exception:
                # Try once more with just the count: without it the day would look unfinished
                # and free up again after daily.STALE_SECONDS.
                try:
                    daily.finish(day, {"leads": len(result.leads)})
                except Exception:
                    pass
            job.update(state="done", result=result, message="Done", pct=100, step=len(STEPS))
        except (PipelineError, GeocodeError) as exc:
            _give_back(day)
            job.update(state="error", message=str(exc))
        except Exception as exc:  # show unexpected failures instead of spinning forever
            _give_back(day)
            job.update(state="error", message=f"Unexpected error: {redact(exc)}")

    def _give_back(day):
        """A search that failed outright does not use up the day."""
        try:
            daily.release(day)
        except Exception:
            pass

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
        return render_template("index.html", defaults={
            "location": config.DEFAULT_LOCATION, "radius": config.DEFAULT_RADIUS_MILES,
            "keywords": ", ".join(config.DEFAULT_KEYWORDS), "min_score": config.DEFAULT_MIN_SCORE,
        }, yelp=yelp_quota(), outcomes=list(calls.OUTCOMES))

    @app.post("/search")
    def search():
        if not _same_origin():
            abort(403)
        try:
            params = _parse_form(request.form)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        job_id = uuid.uuid4().hex[:12]
        job = {"state": "running", "message": "Starting", "params": params}
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
        except store.Unavailable as exc:
            return jsonify({"error": f"Searching needs the database: {exc}."}), 503
        except Exception as exc:
            return jsonify({"error": f"Could not record the search ({exc.__class__.__name__}). "
                                     "Try again."}), 503
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
            with lock:
                jobs.pop(job_id, None)
            _give_back(day)
            return jsonify({"error": "Could not start the search. Try again."}), 503
        return jsonify({"job_id": job_id})

    @app.get("/leads")
    def saved_leads():
        """Every saved lead, for the page to show before any search."""
        try:
            leads = calls.apply(saved.load())
        except store.Unavailable as exc:
            return jsonify({"error": f"Saved leads are off: {exc}."}), 503
        except Exception as exc:
            return jsonify({"error": f"Could not load the saved leads ({exc.__class__.__name__})"}), 503
        return jsonify({"leads": [_lead_json(l) for l in leads]})

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
        except store.Unavailable as exc:
            return jsonify({"error": f"The call can't be saved: {exc}"}), 503
        except Exception as exc:
            return jsonify({"error": f"Could not save the call ({exc.__class__.__name__})"}), 503
        return jsonify({"ok": True, "call": {**call, "when": calls.local_time_text(call["at"])}})

    @app.get("/calls/<uid>")
    def call_history(uid):
        try:
            return jsonify({"calls": calls.history(uid[:64])})
        except Exception as exc:
            return jsonify({"error": f"Could not load the calls ({exc.__class__.__name__})"}), 503

    @app.get("/stats")
    def stats_data():
        try:
            leads = calls.apply(saved.load())
        except Exception as exc:
            return jsonify({"error": f"Could not load the saved leads ({exc.__class__.__name__})"}), 503
        return jsonify(stats.summarize(leads))

    @app.get("/searches")
    def searches():
        """Each day's search (when, what, what it found), and whether today's is used."""
        try:
            body = daily.history()
        except Exception as exc:
            return jsonify({"error": f"Could not load the search history ({exc.__class__.__name__})"}), 503
        with lock:
            running = next((k for k, j in jobs.items() if j["state"] == "running"), None)
        return jsonify({**body, "yelp": yelp_quota(), "running": running})

    @app.post("/mark")
    def mark():
        """Save whether a business has a baler ("yes" or "no"). A mark is kept for good:
        it can be switched but not cleared, and later searches keep it with the business."""
        if not _same_origin():
            abort(403)
        data = request.get_json(silent=True) or {}
        uid, value = str(data.get("key") or ""), str(data.get("value") or "")
        if not uid or len(uid) > 64 or value not in marks.VALUES:
            return jsonify({"error": "Bad mark"}), 400
        try:
            marks.set_mark(uid, value)
        except store.Unavailable as exc:
            return jsonify({"error": f"Marks can't be saved: {exc}"}), 503
        except Exception as exc:
            return jsonify({"error": f"Could not save the mark ({exc.__class__.__name__})"}), 503
        return jsonify({"ok": True})

    @app.get("/status/<job_id>")
    def status(job_id):
        job = jobs.get(job_id) or abort(404)
        body = {"state": job["state"], "message": job["message"], "steps": STEPS,
                "step": job.get("step", 0), "pct": round(job["progress"].pct(), 1),
                "elapsed": round(time.time() - job.get("started", time.time()))}
        if job["state"] == "done":
            res = job["result"]
            leads, stats = res.leads, dict(res.stats)
            warnings, shows_saved = list(res.warnings), False
            if job.get("saved"):
                try:
                    leads = saved.load()
                    stats.update({"new leads saved": job["new_leads"], "saved leads": len(leads)})
                    shows_saved = True
                except Exception as exc:
                    warnings.append(f"Could not load the saved leads ({exc.__class__.__name__}); "
                                    "showing this search only.")
            body.update(leads=[_lead_json(l) for l in leads], stats=stats,
                        warnings=warnings, location=res.location_label,
                        yelp=yelp_quota(), saved=shows_saved)
        return jsonify(body)

    @app.get("/download/<job_id>.<fmt>")
    def download(job_id, fmt):
        """A search's leads, or with job id "saved" every saved lead."""
        if job_id == "saved":
            try:
                leads = calls.apply(saved.load())
            except Exception:
                abort(503)
            info = {"list": "All saved leads", "leads": len(leads)}
        else:
            job = jobs.get(job_id)
            if not job or job["state"] != "done":
                abort(404)
            leads = calls.apply(marks.apply(job["result"].leads))
            info = job["result"].run_info(job["params"])
        if fmt == "csv":
            return Response(to_csv_bytes(leads), mimetype="text/csv",
                            headers={"Content-Disposition": "attachment; filename=compactor-leads.csv"})
        if fmt == "xlsx":
            data = to_xlsx_bytes(leads, info)
            return Response(data, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            headers={"Content-Disposition": "attachment; filename=compactor-leads.xlsx"})
        abort(404)

    return app


if __name__ == "__main__":
    from .envfile import load_dotenv
    load_dotenv()
    create_app().run(debug=True)
