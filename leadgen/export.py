"""CSV and Excel output."""

import csv
import io
import re
import time
from collections.abc import Callable
from typing import Any

from . import places
from .localtime import date_time_text
from .models import Lead
from .pipeline import EXEMPT_TYPES
from .scoring import TIER_LABELS, TIERS
from .xlsx import Book, Cell, clean

# A private scratch column, worded unlike the site's own "Has Baler or Compactor?"
# answer so the two can't be mistaken for each other.
OFFLINE_VERIFIED = "My notes: equipment seen (this file only)"
OFFLINE_CHOICES = ("Saw a compactor", "Saw a baler", "Saw neither", "Not sure")

# The sources in the downloads' words.
SOURCE_NAMES = {"google": "Google", "yelp": "Yelp", "osm": "OpenStreetMap map data"}
# How a category was matched ("+35 Grocery (by map tag): ..."), in the downloads' words.
_HOW = {"Google category": "from the Google listing", "Yelp category": "from the Yelp listing",
        "map tag": "from the map listing", "name": "from its name",
        "search query": "from the search that found it"}


def plain_reason(reason: str) -> str:
    """A score reason in plain words: "(by map tag)" -> "(from the map listing)"."""
    return re.sub(r"\(by ([^)]+)\)", lambda m: f"({_HOW.get(m.group(1), m.group(1))})", reason, count=1)


Column = tuple[str, Callable[[Lead], object], float]

# A listing without a city or ZIP gets the town and ZIP area its map position is near
# (places.py), written "near West Jordan" / "near 84088" so it never passes for an address.
NEAR_NOTE = ("A City or ZIP starting with “near” was worked out from the map position, "
             "because the listing gave none: it is the nearest town / ZIP code area, not an address.")


def _city(lead: Lead) -> str:
    town = places.listed_town(lead)          # tidied: "CLEARFIELD" -> "Clearfield"
    if town:
        return town
    near = places.for_lead(lead)
    return f"near {near.town}" if near else ""


def _state(lead: Lead) -> str:
    if lead.state.strip():
        return places.state_code(lead.state)
    near = places.for_lead(lead)
    return near.state if near else ""


def _zip(lead: Lead) -> str:
    if lead.zip.strip():
        return lead.zip
    near = places.for_lead(lead)
    return f"near {near.zip}" if near and near.zip else ""


# Each column: its heading, the value for a lead, and its width in Excel.
COLUMNS: list[Column] = [
    ("Score", lambda l: l.score, 8),
    ("Tier", lambda l: TIER_LABELS.get(l.tier, l.tier), 13),
    ("Lead Type", lambda l: l.lead_type, 20),
    ("Flags", lambda l: "; ".join(l.flags), 28),
    ("Business Name", lambda l: l.name, 36),
    ("Category", lambda l: l.category, 30),
    ("Address", lambda l: l.address, 30),
    ("City", _city, 20),
    ("State", _state, 7),
    ("ZIP", _zip, 11),
    ("Phone", lambda l: format_phone(l.phone), 16),
    ("Website", lambda l: l.website, 32),
    ("Distance (mi)", lambda l: l.distance_miles, 12),
    ("Why This Score", lambda l: "Points: " + " | ".join(map(plain_reason, l.reasons)) if l.reasons else "", 60),
    ("Matched Keywords", lambda l: ", ".join(l.matched_keywords), 18),
    ("Google Reviews", lambda l: l.rating_count, 10),
    ("Yelp Reviews", lambda l: l.yelp_reviews, 10),
    ("Approx. Footprint (sq ft)", lambda l: l.footprint_sqft, 14),
    ("Source Category", lambda l: l.primary_category, 22),
    ("Found By", lambda l: ", ".join(l.search_terms), 24),
    ("Sources", lambda l: ", ".join(SOURCE_NAMES.get(s, s) for s in (l.sources or [l.source])), 18),
    ("Map Link", lambda l: l.map_url, 30),
    ("Latitude", lambda l: round(l.lat, 6), 11),
    ("Longitude", lambda l: round(l.lon, 6), 11),
    ("Has Baler or Compactor?", lambda l: {"yes": "Yes", "no": "No"}.get(l.has_baler, ""), 13),
    ("Marked By", lambda l: l.marked_by, 16),
    ("Call Result", lambda l: l.call_outcome, 14),
    ("Last Called", lambda l: date_time_text(l.last_call_at), 20),
    ("Called By", lambda l: l.last_call_by, 16),
    # The latest call's notes; when it had none, the latest notes an earlier call has.
    ("Call Notes", lambda l: l.call_notes or (
        f"(From the call on {date_time_text(l.earlier_notes_at)}) {l.earlier_notes}"
        if l.earlier_notes else ""), 40),
    # For notes on a printed or offline copy only: nothing typed here goes back into the
    # website (Yes / No marks and calls are recorded there).
    (OFFLINE_VERIFIED, lambda l: "", 18),
    ("My notes (this file only)", lambda l: "", 30),
]

# Columns left out of a download when every row is empty there (a free map search has
# no reviews, search phrases or calls, say): the others are always there.
OPTIONAL = {"Website", "Matched Keywords", "Google Reviews", "Yelp Reviews",
            "Approx. Footprint (sq ft)", "Source Category", "Found By", "Marked By",
            "Call Result", "Last Called", "Called By", "Call Notes"}


def columns_for(leads: list[Lead]) -> list[Column]:
    """COLUMNS, without the optional ones that are empty in every row."""
    def used(fn: Callable[[Lead], object]) -> bool:
        return any(fn(l) not in (None, "", 0) for l in leads)
    return [c for c in COLUMNS if c[0] not in OPTIONAL or used(c[1])]


TIER_FILLS = {"A": "C6EFCE", "B": "E2EFDA", "C": "FFF2CC", "D": "F2F2F2"}
COMPETITOR_FILL = "F8CBAD"


def format_phone(phone: str | None) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return (phone or "").strip()


_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _safe(value: object) -> object:
    """CSV: neutralize text a spreadsheet would run as a formula (data comes from the public)."""
    if isinstance(value, str):
        value = clean(value)
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def _xl(value: object) -> object:
    """Safe for Excel: no control characters a spreadsheet can't hold."""
    return clean(value) if isinstance(value, str) else value


def rows(leads: list[Lead], columns: list[Column] | None = None) -> list[list[object]]:
    return [[fn(l) for _, fn, _ in (columns or COLUMNS)] for l in leads]


def to_csv_bytes(leads: list[Lead]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    columns = columns_for(leads)
    w.writerow([c for c, _, _ in columns])
    for r in rows(leads, columns):
        w.writerow(["" if v is None else _safe(v) for v in r])
    return buf.getvalue().encode("utf-8-sig")   # BOM so Excel opens it cleanly


def to_xlsx_bytes(leads: list[Lead], run_info: dict[str, Any] | None = None) -> bytes:
    """The leads as an Excel file, plus a Run Info sheet (run_info: label -> value).

    Written by xlsx.py, in time that grows in step with the number of leads.
    """
    book = Book()
    columns = columns_for(leads)
    ws = book.add_sheet("Leads", [width for _, _, width in columns], freeze="F2")
    ws.append([Cell(name, fill="1F4E78", font="header", wrap=True) for name, _, _ in columns])
    site_at, map_at = _col("Website", columns) - 1, _col("Map Link", columns) - 1
    for lead in leads:
        row = [_xl(fn(lead)) for _, fn, _ in columns]
        fill = COMPETITOR_FILL if lead.lead_type == "Competitor" else TIER_FILLS.get(lead.tier)
        if fill:
            for i in range(4):
                row[i] = Cell(row[i], fill=fill)
        if site_at >= 0 and re.match(r"https?://", lead.website or "", re.I):
            row[site_at] = Cell(row[site_at], font="link", link=lead.website)
        if re.match(r"https?://", lead.map_url or "", re.I):
            row[map_at] = Cell("Open map", font="link", link=lead.map_url)
        ws.append(row)
    ws.filter = True
    ws.dropdown = (_col(OFFLINE_VERIFIED, columns), OFFLINE_CHOICES)

    info = book.add_sheet("Run Info", [26, 90])
    info.append(["Generated", date_time_text(time.time()) + " (Utah time)"])
    for k, v in (run_info or {}).items():
        info.append([_xl(str(k)), _xl(v if isinstance(v, (int, float, str)) else str(v))])
    info.append([])
    info.append(["Tiers", tier_text()])
    info.append(["Row colors", "Green = stronger lead, orange = competitor"])
    if any(_city(l).startswith("near ") or _zip(l).startswith("near ") for l in leads):
        info.append(["City / ZIP “near …”", NEAR_NOTE])
    left_out = [name for name, _, _ in COLUMNS if name not in {c for c, _, _ in columns}]
    if left_out:
        info.append(["Columns left out", "Empty for every lead in this file: " + ", ".join(left_out)])
    info.append(["This file only", "The last two columns are for your own notes on this "
                 "copy. Nothing typed there is saved in the website: record Yes / No and "
                 "calls on the Leads page."])
    return book.save()


def saved_list_info(leads: list[Lead], first: float | None = None,
                    latest: float | None = None, searched: bool = True) -> dict[str, Any]:
    """The Run Info of the saved list's download: what the file holds, in plain labels.
    first / latest: when the first and latest search started (epoch seconds, as the
    search history shows them), or with searched=False when leads were first and
    last saved (no search history to go by)."""
    prospects = [l for l in leads if l.lead_type not in EXEMPT_TYPES]
    info = {
        "List": "All saved leads",
        "Leads in this file": len(leads),
        "Marked Yes (has a baler or compactor)": sum(l.has_baler == "yes" for l in prospects),
        "Marked No": sum(l.has_baler == "no" for l in prospects),
        "Not checked yet": sum(l.has_baler not in ("yes", "no") for l in prospects),
        "Competitors and Arco's own listing": len(leads) - len(prospects),
    }
    for tier, label in TIER_LABELS.items():
        info[f"Tier {label}"] = sum(l.tier == tier for l in leads)
    info["Called at least once"] = sum(bool(l.call_count) for l in leads)
    what = "search" if searched else "saved"
    if first:
        info[f"First {what}"] = date_time_text(first) + " (Utah time)"
    if latest:
        info[f"Latest {what}"] = date_time_text(latest) + " (Utah time)"
    return info


def tier_text() -> str:
    """The tier boundaries in words, from scoring.TIERS: "A >= 60, B >= 40, ..."."""
    parts = [f"{tier} >= {low}" for low, tier in TIERS if low > 0]
    lowest = min(TIERS)
    above = min(low for low, _ in TIERS if low > lowest[0])
    return ", ".join(parts + [f"{lowest[1]} below {above}"])


def _col(name: str, columns: list[Column]) -> int:
    """The column's number (from 1) in columns; 0 when it was left out."""
    return next((i for i, (c, _, _) in enumerate(columns, start=1) if c == name), 0)
