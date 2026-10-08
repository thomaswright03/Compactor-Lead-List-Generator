"""The Find leads form, checked strictly on the server before anything is spent: each
refusal (FormError) says what is wrong and names the field the page shows it next to."""

import math
from collections.abc import Mapping
from typing import Any

from .. import config
from ..pipeline import GRIDS, SOURCES, SearchParams

Num = int | float

MAX_KEYWORDS = 20
MAX_KEYWORD_LEN = 60
MAX_REQUESTS_LIMIT = 5000


class FormError(ValueError):
    """A form value the server refuses; `field` names the input the page shows it next to."""

    def __init__(self, message: str, field: str = "") -> None:
        super().__init__(message)
        self.field = field


def _number(form: Mapping[str, str], name: str, default: Num | None, cast: type, lo: float, hi: float,
            label: str, unit: str = "") -> Any:
    """A number field; its errors use the field's on-screen label (and unit)."""
    raw = (form.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise FormError(f"{label} must be a number.", name) from None
    if cast is int:
        if not math.isfinite(value) or not value.is_integer():
            raise FormError(f"{label} must be a whole number.", name)
        value = int(value)
    if not lo <= value <= hi:
        raise FormError(f"{label} must be between {lo:g} and {hi:g}{unit}.", name)
    return value


def _location(form: Mapping[str, str]) -> str:
    # The page always sends the field; a bare POST without it (a script) searches around AARCO.
    location = (form.get("location", config.DEFAULT_LOCATION) or "").strip()
    if not location:
        raise FormError("Enter where to search around: a ZIP code, city or street address.",
                        "location")
    if len(location) > 200:
        raise FormError("Search around is too long: use at most 200 characters.", "location")
    return location


def parse_form(form: Mapping[str, str]) -> SearchParams:
    """Validate the form strictly; raise FormError with a message for the page (and the
    field it is about)."""
    location = _location(form)
    keywords = [k.strip() for k in form.get("keywords", "").split(",") if k.strip()]
    if len(keywords) > MAX_KEYWORDS:
        raise FormError(f"Use at most {MAX_KEYWORDS} search words (you have {len(keywords)}).",
                        "keywords")
    long = next((k for k in keywords if len(k) > MAX_KEYWORD_LEN), None)
    if long is not None:
        raise FormError(f"Each search word can be up to {MAX_KEYWORD_LEN} characters; "
                        f"“{long[:20]}…” has {len(long)}. Separate words with commas.",
                        "keywords")
    source = form.get("source", "auto")
    if source not in SOURCES:
        raise FormError("Pick where to search from the list.", "source")
    # The page always sends How far and the minimum score: a blank one is a mistake to
    # point out, never a silent default (a script that leaves them out gets the defaults).
    for name, message in (("radius", "How far must be between 1 and 100 miles."),
                          ("min_score", "The score to leave out weak leads below must be "
                                        "between 0 and 100.")):
        if name in form and not (form.get(name) or "").strip():
            raise FormError(message, name)
    grid = _number(form, "grid", 1, int, 1, 19, "Coverage")
    if grid not in GRIDS:
        raise FormError("Pick a coverage from the list.", "grid")
    return SearchParams(
        location=location,
        radius_miles=_number(form, "radius", config.DEFAULT_RADIUS_MILES, float, 1, 100,
                             "How far", " miles"),
        # The standard words are always searched; the ones typed are searched as well.
        keywords=list(dict.fromkeys(config.DEFAULT_KEYWORDS
                                    + [k for k in keywords if k.lower() not in config.DEFAULT_KEYWORDS])),
        source=source,
        min_score=_number(form, "min_score", config.DEFAULT_MIN_SCORE, int, 0, 100,
                          "The score to leave out weak leads below"),
        grid=grid,
        max_requests=_number(form, "max_requests", None, int, 1, MAX_REQUESTS_LIMIT,
                             "Most paid lookups for this search"),
        only_keyword_matches=form.get("only_keyword_matches") == "on",
    )
