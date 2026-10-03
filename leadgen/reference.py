"""Copy the businesses AARCO's staff marked Yes / No into the scoring reference set.

    python -m leadgen reference                  # DATABASE_URL = the website's database

or, with no database address needed, download the same file from the site (Find leads,
"For the site administrator", Scoring check file: /download/scoring-reference.json) and
put it in place of tests/fixtures/scoring_reference.json.

tests/fixtures/scoring_reference.json keeps typical examples ("businesses") and,
under "confirmed", real businesses marked on the Leads page, each with the tier
it had when it was copied. The tests fail if a scoring change moves a business
marked Yes to a lower tier, or one marked No to a higher one. Only the facts
the scoring reads are copied (no phone numbers, addresses or call notes).
"""

import json
from pathlib import Path
from typing import Any

from . import marks, saved
from .models import Lead
from .pipeline import EXEMPT_TYPES

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "scoring_reference.json"
FACTS = ("name", "source", "raw_categories", "primary_category", "rating_count", "yelp_reviews",
         "footprint_sqft", "website", "alt_names", "search_terms")


def confirmed_entries(leads: list[Lead]) -> list[dict[str, Any]]:
    """Reference entries for the marked prospects among leads (marks applied)."""
    out = []
    for lead in leads:
        if lead.has_baler not in marks.VALUES or lead.lead_type in EXEMPT_TYPES:
            continue
        entry = {k: getattr(lead, k) for k in FACTS if getattr(lead, k) not in (None, "", [])}
        entry.update({"marked": lead.has_baler, "tier": lead.tier, "uid": lead.uid})
        out.append(entry)
    return sorted(out, key=lambda e: (e["marked"], e["name"].lower()))


def build(path: str | Path = DEFAULT_PATH) -> tuple[dict[str, Any], int, int]:
    """The reference file with its "confirmed" list replaced by the marks saved now:
    (its data, how many marked Yes, how many No). Raises store.Unavailable without a
    database."""
    path = Path(path)
    data = json.loads(path.read_text()) if path.exists() else {"businesses": []}
    entries = confirmed_entries(marks.apply(saved.load()))
    data["confirmed"] = entries
    yes = sum(e["marked"] == "yes" for e in entries)
    return data, yes, len(entries) - yes


def dumps(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=1, ensure_ascii=False) + "\n"


def export(path: str | Path = DEFAULT_PATH) -> tuple[int, int]:
    """Replace the "confirmed" list in the reference file with the marks saved now.
    Returns (yes, no) counts. Raises store.Unavailable without a database."""
    data, yes, no = build(path)
    Path(path).write_text(dumps(data))
    return yes, no
