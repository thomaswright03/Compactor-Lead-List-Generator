"""Numbers for the stats page, from the saved leads and their Yes / No marks.

They measure how well the scoring finds prospects, so competitors and Arco's own
listing (flagged in the list, never asked Yes / No) are left out of every figure.
A business that has since closed for good still counts: its mark says what it
had while it was open, which is what the tiers are measured on.
"""

from typing import Any

from .models import Lead
from .pipeline import EXEMPT_TYPES
from .saved import is_closed
from .scoring import TIERS

TIER_ORDER = ("A", "B", "C", "D")


def summarize(leads: list[Lead]) -> dict[str, Any]:
    """leads: saved leads with has_baler set."""
    prospects = [l for l in leads if l.lead_type not in EXEMPT_TYPES]
    left_out = len(leads) - len(prospects)
    leads = prospects
    yes = [l for l in leads if l.has_baler == "yes"]
    checked = [l for l in leads if l.has_baler in ("yes", "no")]
    avg = round(sum(l.score for l in yes) / len(yes)) if yes else None
    by_tier = []
    for tier in TIER_ORDER:
        asked = [l for l in checked if l.tier == tier]
        have = sum(l.has_baler == "yes" for l in asked)
        by_tier.append({"tier": tier, "checked": len(asked), "yes": have,
                        "pct": round(100 * have / len(asked)) if asked else None,
                        # Saved in this tier, checked or not (the page's note on tier D).
                        "saved": sum(l.tier == tier for l in leads)})
    return {
        "with_equipment": len(yes),
        "checked": len(checked),
        "average_score": avg,
        "average_tier": next((t for threshold, t in TIERS if avg >= threshold), "D")
                        if avg is not None else None,
        "by_tier": by_tier,
        "left_out": left_out,
        "closed": sum(is_closed(l) for l in checked),
        # The lowest score of every tier but D, for the page's note about tier D.
        "tier_floors": {t: threshold for threshold, t in TIERS},
    }
