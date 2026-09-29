"""CSV and Excel output."""

import csv
import io
import re
import time

from .localtime import date_time_text
from .pipeline import EXEMPT_TYPES
from .scoring import TIER_LABELS, TIERS
from .xlsx import Book, Cell, clean

# A private scratch column, worded unlike the site's own "Has Baler or Compactor?"
# answer so the two can't be mistaken for each other.
OFFLINE_VERIFIED = "My notes: equipment seen (this file only)"
OFFLINE_CHOICES = ("Saw a compactor", "Saw a baler", "Saw neither", "Not sure")

COLUMNS = [
    ("Score", lambda l: l.score, 8),
    ("Tier", lambda l: TIER_LABELS.get(l.tier, l.tier), 13),
    ("Lead Type", lambda l: l.lead_type, 20),
    ("Flags", lambda l: "; ".join(l.flags), 28),
    ("Business Name", lambda l: l.name, 36),
    ("Category", lambda l: l.category, 30),
    ("Address", lambda l: l.address, 30),
    ("City", lambda l: l.city, 18),
    ("State", lambda l: l.state, 7),
    ("ZIP", lambda l: l.zip, 8),
    ("Phone", lambda l: format_phone(l.phone), 16),
    ("Website", lambda l: l.website, 32),
    ("Distance (mi)", lambda l: l.distance_miles, 12),
    ("Why This Score", lambda l: "Points: " + " | ".join(l.reasons) if l.reasons else "", 60),
    ("Matched Keywords", lambda l: ", ".join(l.matched_keywords), 18),
    ("Google Reviews", lambda l: l.rating_count, 10),
    ("Yelp Reviews", lambda l: l.yelp_reviews, 10),
    ("Approx. Footprint (sq ft)", lambda l: l.footprint_sqft, 14),
    ("Source Category", lambda l: l.primary_category, 22),
    ("Found By", lambda l: ", ".join(l.search_terms), 24),
    ("Sources", lambda l: ", ".join(l.sources or [l.source]), 12),
    ("Map Link", lambda l: l.map_url, 30),
    ("Latitude", lambda l: round(l.lat, 6), 11),
    ("Longitude", lambda l: round(l.lon, 6), 11),
    ("Has Baler or Compactor?", lambda l: {"yes": "Yes", "no": "No"}.get(l.has_baler, ""), 13),
    ("Call Result", lambda l: l.call_outcome, 14),
    ("Last Called", lambda l: date_time_text(l.last_call_at), 20),
    ("Call Notes", lambda l: l.call_notes, 40),
    # For notes on a printed or offline copy only: nothing typed here goes back into the
    # website (Yes / No marks and calls are recorded there).
    (OFFLINE_VERIFIED, lambda l: "", 18),
    ("My notes (this file only)", lambda l: "", 30),
]

TIER_FILLS = {"A": "C6EFCE", "B": "E2EFDA", "C": "FFF2CC", "D": "F2F2F2"}
COMPETITOR_FILL = "F8CBAD"


def format_phone(phone):
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return (phone or "").strip()


_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _safe(value):
    """CSV: neutralize text a spreadsheet would run as a formula (data comes from the public)."""
    if isinstance(value, str):
        value = clean(value)
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def _xl(value):
    """Safe for Excel: no control characters a spreadsheet can't hold."""
    return clean(value) if isinstance(value, str) else value


def rows(leads):
    return [[fn(l) for _, fn, _ in COLUMNS] for l in leads]


def to_csv_bytes(leads):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([c for c, _, _ in COLUMNS])
    for r in rows(leads):
        w.writerow(["" if v is None else _safe(v) for v in r])
    return buf.getvalue().encode("utf-8-sig")   # BOM so Excel opens it cleanly


def to_xlsx_bytes(leads, run_info=None):
    """The leads as an Excel file, plus a Run Info sheet (run_info: label -> value).

    Written by xlsx.py, in time that grows in step with the number of leads.
    """
    book = Book()
    ws = book.add_sheet("Leads", [width for _, _, width in COLUMNS], freeze="F2")
    ws.append([Cell(name, fill="1F4E78", font="header", wrap=True) for name, _, _ in COLUMNS])
    site_at, map_at = _col("Website") - 1, _col("Map Link") - 1
    for lead in leads:
        row = [_xl(fn(lead)) for _, fn, _ in COLUMNS]
        fill = COMPETITOR_FILL if lead.lead_type == "Competitor" else TIER_FILLS.get(lead.tier)
        if fill:
            for i in range(4):
                row[i] = Cell(row[i], fill=fill)
        if re.match(r"https?://", lead.website or "", re.I):
            row[site_at] = Cell(row[site_at], font="link", link=lead.website)
        if re.match(r"https?://", lead.map_url or "", re.I):
            row[map_at] = Cell("Open map", font="link", link=lead.map_url)
        ws.append(row)
    ws.filter = True
    ws.dropdown = (_col(OFFLINE_VERIFIED), OFFLINE_CHOICES)

    info = book.add_sheet("Run Info", [26, 90])
    info.append(["Generated", date_time_text(time.time()) + " (Utah time)"])
    for k, v in (run_info or {}).items():
        info.append([_xl(str(k)), _xl(v if isinstance(v, (int, float, str)) else str(v))])
    info.append([])
    info.append(["Tiers", tier_text()])
    info.append(["Row colors", "Green = stronger lead, orange = competitor"])
    info.append(["This file only", "The last two columns are for your own notes on this "
                 "copy. Nothing typed there is saved in the website: record Yes / No and "
                 "calls on the Leads page."])
    return book.save()


def saved_list_info(leads, first=None, latest=None):
    """The Run Info of the saved list's download: what the file holds, in plain labels.
    first / latest: when the first and latest search ran (epoch seconds)."""
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
    if first:
        info["First search"] = date_time_text(first) + " (Utah time)"
    if latest:
        info["Latest search"] = date_time_text(latest) + " (Utah time)"
    return info


def tier_text():
    """The tier boundaries in words, from scoring.TIERS: "A >= 60, B >= 40, ..."."""
    parts = [f"{tier} >= {low}" for low, tier in TIERS if low > 0]
    lowest = min(TIERS)
    above = min(low for low, _ in TIERS if low > lowest[0])
    return ", ".join(parts + [f"{lowest[1]} below {above}"])


def _col(name):
    return next(i for i, (c, _, _) in enumerate(COLUMNS, start=1) if c == name)
