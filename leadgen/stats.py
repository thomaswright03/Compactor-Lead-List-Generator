"""Numbers for the stats page, from the saved leads and their Yes / No marks."""

from .scoring import TIERS

TIER_ORDER = ("A", "B", "C", "D")


def summarize(leads):
    """leads: saved leads with has_baler set."""
    yes = [l for l in leads if l.has_baler == "yes"]
    checked = [l for l in leads if l.has_baler in ("yes", "no")]
    avg = round(sum(l.score for l in yes) / len(yes)) if yes else None
    by_tier = []
    for tier in TIER_ORDER:
        asked = [l for l in checked if l.tier == tier]
        have = sum(l.has_baler == "yes" for l in asked)
        by_tier.append({"tier": tier, "checked": len(asked), "yes": have,
                        "pct": round(100 * have / len(asked)) if asked else None})
    return {
        "with_equipment": len(yes),
        "checked": len(checked),
        "average_score": avg,
        "average_tier": next((t for threshold, t in TIERS if avg >= threshold), "D")
                        if avg is not None else None,
        "by_tier": by_tier,
    }
