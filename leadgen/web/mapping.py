"""The Map page's data: AARCO's area and its search areas, and a pin per saved lead
(area_map.py). Read only; behind the login like every page."""

import logging
import time

from flask import Blueprint, Response, jsonify
from flask.typing import ResponseReturnValue

from .. import area_map
from .common import LoadError, load_saved

log = logging.getLogger("leadgen.web")

bp = Blueprint("mapping", __name__)


@bp.get("/map-data")
def map_data() -> ResponseReturnValue:
    """Everything the Map page draws (area_map.page): the circle and areas, how the
    latest search of AARCO's area covered them, and every saved lead with a map position
    as a short list of fields ("fields" names them). The saved list is the one the
    Leads page reads (kept parsed between requests), so this stays quick for thousands
    of leads; a lead's mark and latest call come with it."""
    try:
        leads, _ = load_saved()
    except LoadError as exc:
        return jsonify({"error": str(exc)}), 503
    unread = False
    try:
        record = area_map.latest_search()
    except Exception:
        # Only the shading needs it: the map is still worth showing without.
        log.warning("Reading the latest search for the map failed", exc_info=True)
        record, unread = None, True
    body = area_map.page(leads, record, unread)
    body["now"] = time.time()
    return Response(area_map.dumps(body), mimetype="application/json")
