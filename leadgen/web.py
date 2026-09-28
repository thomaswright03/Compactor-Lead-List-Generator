"""Minimal web page: fill in the form, watch progress, preview, download CSV/Excel."""

import threading
import uuid
from collections import OrderedDict

from flask import Flask, Response, abort, jsonify, render_template, request

from . import config
from .export import format_phone, to_csv_bytes, to_xlsx_bytes
from .geo import GeocodeError
from .pipeline import PipelineError, SearchParams, run
from .scoring import TIER_LABELS

MAX_JOBS = 20


def _lead_json(lead):
    return {
        "score": lead.score, "tier": lead.tier, "tier_label": TIER_LABELS.get(lead.tier, ""),
        "lead_type": lead.lead_type, "flags": lead.flags, "name": lead.name,
        "category": lead.category, "address": lead.address, "city": lead.city,
        "zip": lead.zip, "phone": format_phone(lead.phone), "website": lead.website,
        "distance": lead.distance_miles, "reasons": lead.reasons, "map_url": lead.map_url,
        "sources": lead.sources,
    }


def create_app():
    app = Flask(__name__)
    jobs = OrderedDict()
    lock = threading.Lock()

    def _parse_form(form):
        def num(name, default, cast=float):
            try:
                return cast(form.get(name, "") or default)
            except ValueError:
                return default
        keywords = [k.strip() for k in form.get("keywords", "").split(",") if k.strip()]
        return SearchParams(
            location=form.get("location", "").strip() or config.DEFAULT_LOCATION,
            radius_miles=num("radius", config.DEFAULT_RADIUS_MILES),
            keywords=keywords,
            source=form.get("source", "auto"),
            min_score=num("min_score", config.DEFAULT_MIN_SCORE, int),
            grid=num("grid", 1, int),
            max_requests=num("max_requests", 150, int),
            only_keyword_matches=form.get("only_keyword_matches") == "on",
        )

    def _worker(job_id, params):
        job = jobs[job_id]

        def progress(msg):
            job["message"] = msg

        try:
            result = run(params, progress)
            job.update(state="done", result=result, message="Done")
        except (PipelineError, GeocodeError) as exc:
            job.update(state="error", message=str(exc))
        except Exception as exc:  # show unexpected failures instead of spinning forever
            job.update(state="error", message=f"Unexpected error: {exc}")

    @app.get("/")
    def index():
        return render_template("index.html", defaults={
            "location": config.DEFAULT_LOCATION, "radius": config.DEFAULT_RADIUS_MILES,
            "keywords": ", ".join(config.DEFAULT_KEYWORDS), "min_score": config.DEFAULT_MIN_SCORE,
        })

    @app.post("/search")
    def search():
        params = _parse_form(request.form)
        job_id = uuid.uuid4().hex[:12]
        with lock:
            jobs[job_id] = {"state": "running", "message": "Starting", "params": params}
            while len(jobs) > MAX_JOBS:
                jobs.popitem(last=False)
        threading.Thread(target=_worker, args=(job_id, params), daemon=True).start()
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
