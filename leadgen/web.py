"""Minimal web page: fill in the form, watch progress, preview, download CSV/Excel."""

import hmac
import ipaddress
import math
import os
import threading
import uuid
from collections import OrderedDict
from urllib.parse import urlparse

from flask import Flask, Response, abort, jsonify, render_template, request

from . import config
from .export import format_phone, to_csv_bytes, to_xlsx_bytes
from .geo import GeocodeError
from .http import redact
from .pipeline import GRIDS, SOURCES, PipelineError, SearchParams, run
from .scoring import TIER_LABELS

MAX_JOBS = 20
MAX_KEYWORDS = 20
MAX_KEYWORD_LEN = 60
MAX_REQUESTS_LIMIT = 5000


def _lead_json(lead):
    return {
        "score": lead.score, "tier": lead.tier, "tier_label": TIER_LABELS.get(lead.tier, ""),
        "lead_type": lead.lead_type, "flags": lead.flags, "name": lead.name,
        "category": lead.category, "address": lead.address, "city": lead.city,
        "zip": lead.zip, "phone": format_phone(lead.phone), "website": lead.website,
        "distance": lead.distance_miles, "reasons": lead.reasons, "map_url": lead.map_url,
        "sources": lead.sources,
    }


def create_app(password=None):
    """password (or the APP_PASSWORD env var) puts the whole site behind a login.

    Always set one when the page is reachable from the internet: every search
    can spend the Google or Yelp API key.
    """
    app = Flask(__name__)
    password = password if password is not None else os.environ.get("APP_PASSWORD", "")
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

    @app.before_request
    def require_login():
        if not password:
            # Without a login only local/IP access is allowed (see LEADGEN_ALLOWED_HOSTS).
            return None if _host_allowed() else abort(403)
        auth = request.authorization
        supplied = (auth.password or "") if auth else ""
        if hmac.compare_digest(supplied.encode(), password.encode()):
            return None
        return Response("Login required", 401,
                        {"WWW-Authenticate": 'Basic realm="Lead Finder", charset="UTF-8"'})

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

    def _worker(job, params):
        def progress(msg):
            job["message"] = msg

        try:
            result = run(params, progress)
            job.update(state="done", result=result, message="Done")
        except (PipelineError, GeocodeError) as exc:
            job.update(state="error", message=str(exc))
        except Exception as exc:  # show unexpected failures instead of spinning forever
            job.update(state="error", message=f"Unexpected error: {redact(exc)}")

    def _same_origin():
        """Stop other websites from starting (billed) searches through this server."""
        site = request.headers.get("Sec-Fetch-Site")
        if site not in (None, "same-origin", "none"):
            return False
        origin = request.headers.get("Origin") or request.headers.get("Referer")
        return not origin or urlparse(origin).netloc == request.host

    @app.get("/")
    def index():
        return render_template("index.html", defaults={
            "location": config.DEFAULT_LOCATION, "radius": config.DEFAULT_RADIUS_MILES,
            "keywords": ", ".join(config.DEFAULT_KEYWORDS), "min_score": config.DEFAULT_MIN_SCORE,
        })

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
        with lock:
            running = next((k for k, j in jobs.items() if j["state"] == "running"), None)
            if running is not None:
                # The page attaches to it (e.g. after a reload) instead of starting another.
                return jsonify({"error": "A search is already running.", "job_id": running}), 429
            jobs[job_id] = job
            # Evict the oldest finished jobs; never a running one.
            while len(jobs) > MAX_JOBS:
                old = next((k for k, v in jobs.items() if v["state"] != "running"), None)
                if old is None:
                    break
                del jobs[old]
        try:
            threading.Thread(target=_worker, args=(job, params), daemon=True).start()
        except RuntimeError:
            with lock:
                jobs.pop(job_id, None)
            return jsonify({"error": "Could not start the search. Try again."}), 503
        return jsonify({"job_id": job_id})

    @app.get("/status/<job_id>")
    def status(job_id):
        job = jobs.get(job_id) or abort(404)
        body = {"state": job["state"], "message": job["message"]}
        if job["state"] == "done":
            res = job["result"]
            body.update(leads=[_lead_json(l) for l in res.leads], stats=res.stats,
                        warnings=res.warnings, location=res.location_label)
        return jsonify(body)

    @app.get("/download/<job_id>.<fmt>")
    def download(job_id, fmt):
        job = jobs.get(job_id)
        if not job or job["state"] != "done":
            abort(404)
        res = job["result"]
        if fmt == "csv":
            return Response(to_csv_bytes(res.leads), mimetype="text/csv",
                            headers={"Content-Disposition": "attachment; filename=compactor-leads.csv"})
        if fmt == "xlsx":
            data = to_xlsx_bytes(res.leads, res.run_info(job["params"]))
            return Response(data, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            headers={"Content-Disposition": "attachment; filename=compactor-leads.xlsx"})
        abort(404)

    return app


if __name__ == "__main__":
    from .envfile import load_dotenv
    load_dotenv()
    create_app().run(debug=True)
