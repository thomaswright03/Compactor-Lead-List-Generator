"""Where a search will run, found before anything is spent: GET /place answers the
page's confirmation (the place found for what was typed, and how far it is from AARCO's
shop), and POST /search (web/finding.py) checks the same before it claims the day. A
place outside AARCO's area, or a doubtful match (only a road, a trail, a park or a
business: geo.Place.doubt), needs the page's explicit yes (confirmed) before the day's
one search is spent on it."""

import logging
from typing import Any

from flask import Blueprint, Response, jsonify, request
from flask.typing import ResponseReturnValue

from .. import company
from ..geo import GeocodeError, LookupDown, Place, haversine_miles, miles_from_aarco
from ..pipeline import SearchParams, locate
from .search_form import FormError, _location

log = logging.getLogger("leadgen.web")
bp = Blueprint("place", __name__)

# A confirmation (of a far or doubtful place) is for the place it named: within this
# many miles of it.
CONFIRM_MILES = 1.0


def where(params: SearchParams) -> dict[str, Any]:
    """Where a search will actually run, for the page to show before it starts: the
    place found for the location typed, how far it is from AARCO's shop, whether that
    is outside AARCO's area (config.SERVICE_AREA_MILES), and "uncertain": why the match
    may not be the place meant (geo.Place.doubt; "" for a town, a ZIP code, a street
    address or a place known offline). Raises GeocodeError."""
    found = locate(params)
    lat, lon, label = found
    miles, area = miles_from_aarco(lat, lon), company.current().area_miles
    return {"label": label, "lat": round(lat, 5), "lon": round(lon, 5),
            "miles": round(miles, 1), "outside": miles > area,
            "area_miles": area, "confirm": f"{lat:.5f},{lon:.5f}",
            "uncertain": found.doubt if isinstance(found, Place) else ""}


def far_message(place: dict[str, Any]) -> str:
    own = company.current().short_name
    return (f"“{place['label']}” is {place['miles']:,.0f} miles from {own}'s shop, outside "
            f"{own}'s area (about {place['area_miles']:g} miles around it).")


def doubt_message(place: dict[str, Any]) -> str:
    return f"{place['uncertain']} Check the place, or confirm that you want to search there."


def confirmed(answer: str | None, place: dict[str, Any]) -> bool:
    """True when the page confirmed searching this far or doubtful place (the place it
    showed)."""
    try:
        lat, lon = (float(x) for x in (answer or "").split(","))
    except ValueError:
        return False
    return haversine_miles(lat, lon, place["lat"], place["lon"]) <= CONFIRM_MILES


def _place(params: SearchParams) -> dict[str, Any] | tuple[Response, int]:
    """The place a search will run around, or the response for the page when it
    can't be found (nothing is spent then)."""
    try:
        return where(params)
    except LookupDown as exc:
        # The lookup services are down: not a mistake in what was typed (no field).
        log.warning("Looking up the place to search failed: %s", exc)
        return jsonify({"error": str(exc), "lookup_down": True}), 503
    except GeocodeError as exc:
        return jsonify({"error": str(exc), "field": "location"}), 400
    except Exception:
        log.exception("Looking up the place to search failed")
        return jsonify({"error": "Couldn't look up the place to search around just now. "
                                 "Nothing was spent. Try again in a minute."}), 503


@bp.get("/place")
def place() -> ResponseReturnValue:
    """Where a search for ?location= would run, and how far that is from AARCO (the
    page shows it in the confirmation before anything is spent)."""
    try:
        params = SearchParams(location=_location(request.args))
    except FormError as exc:
        return jsonify({"error": str(exc), "field": exc.field}), 400
    found = _place(params)
    return found if isinstance(found, tuple) else jsonify(found)
