"""How the scoring does on the reference set: `python -m leadgen scoring-report`.

The reference set (tests/fixtures/scoring_reference.json, see reference.py) holds
typical listings with whether each runs a baler or compactor, and the real businesses
AARCO's staff marked Yes / No on the site, each with the tier it had when it was
copied. The report scores every one with the rules as they are now and prints, per
tier, how many there are, how many are known to have equipment (or were marked Yes),
how many are flagged, and which confirmed businesses moved tier since the file was
copied. docs/scoring-evaluation.md explains the numbers.
"""

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import config
from .models import Lead
from .reference import DEFAULT_PATH, FACTS
from .scoring import TIERS, score_lead

TIER_ORDER = [tier for _, tier in TIERS]


def _lead(item: dict[str, Any]) -> Lead:
    """A scored lead from a reference entry (only the facts the scoring reads)."""
    facts = {k: item[k] for k in FACTS if k in item}
    facts.setdefault("source", "osm")
    lead = Lead(lat=40.7, lon=-111.9, source_id=item["name"], **facts)
    score_lead(lead, config.DEFAULT_KEYWORDS)
    return lead


def _table(rows: Sequence[tuple[str, ...]]) -> list[str]:
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    return ["  ".join(cell.ljust(widths[i]) if i == 0 else cell.rjust(widths[i]) for i, cell in enumerate(r))
            for r in rows]


def typical(items: list[dict[str, Any]]) -> list[str]:
    """Per tier: listings, known to have equipment, known not to, flagged (competitor
    or own listing); then the two facts the tests hold the scoring to."""
    scored = [(bool(item.get("has_equipment")), _lead(item)) for item in items]
    rows = [("Tier", "Businesses", "Have equipment", "Don't", "Flagged")]
    for tier in TIER_ORDER:
        these = [(has, l) for has, l in scored if l.tier == tier]
        rows.append((tier, str(len(these)), str(sum(has for has, _ in these)),
                     str(sum(not has for has, _ in these)), str(sum(bool(l.flags) for _, l in these))))
    have = [l for has, l in scored if has]
    lack = [l for has, l in scored if not has]
    strong = sum(l.tier in ("A", "B") for l in have)
    lines = _table(rows)
    if have:
        lines.append(f"Known to have equipment in tier A or B: {strong} of {len(have)} "
                     f"({100 * strong // len(have)}%; the tests need 75%)")
    if have and lack:
        lines.append(f"Lowest score with equipment: {min(l.score for l in have)}; highest without: "
                     f"{max(l.score for l in lack)} (the tests need the first above the second)")
    return lines


def confirmed(items: list[dict[str, Any]]) -> list[str]:
    """Per tier: the businesses staff marked, how many Yes and No, how many moved tier
    since the file was copied; then each one that moved."""
    if not items:
        return ["None yet: the site's Yes / No marks haven't been copied in (operator runbook, "
                "\"Monthly: the scoring check\")."]
    scored = [(item, _lead(item)) for item in items]
    rows = [("Tier", "Businesses", "Marked Yes", "Marked No", "Moved tier")]
    for tier in TIER_ORDER:
        these = [(item, l) for item, l in scored if l.tier == tier]
        rows.append((tier, str(len(these)), str(sum(item["marked"] == "yes" for item, _ in these)),
                     str(sum(item["marked"] == "no" for item, _ in these)),
                     str(sum(item.get("tier") != l.tier for item, l in these))))
    lines = _table(rows)
    moved = [f"  {item['name']} (marked {item['marked']}): {item.get('tier', '?')} -> {l.tier}"
             for item, l in scored if item.get("tier") != l.tier]
    lines.append("Moved tier since the file was copied: " + ("none" if not moved else ""))
    lines += moved
    return lines


def report(path: str | Path = DEFAULT_PATH) -> str:
    data = json.loads(Path(path).read_text())
    others = {k: len(v) for k, v in data.items() if k not in ("about", "businesses", "confirmed")
              and isinstance(v, list)}
    lines = [f"Scoring rules version {config.SCORING_VERSION} (config.SCORING_VERSION) on {path}", "",
             f"Typical listings ({len(data.get('businesses', []))}, with whether each runs a baler or compactor)",
             *typical(data.get("businesses", [])), "",
             f"Businesses AARCO's staff marked Yes / No on the site ({len(data.get('confirmed', []))})",
             *confirmed(data.get("confirmed", []))]
    if others:
        lines += ["", "Also held to by the tests: " + ", ".join(f"{k} ({n})" for k, n in others.items())]
    return "\n".join(lines)
