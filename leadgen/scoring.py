"""Relevance scoring: how likely is this location to run a large compactor or baler?

Score (0-100) = category weight + brand bonus + size bonus + keyword bonus.
Every point is explained in `lead.reasons` so the client can see why a lead
ranked where it did and tell us which rules to adjust.
"""

import re

from . import config
from .models import Lead

TIERS = [(60, "A"), (40, "B"), (20, "C"), (0, "D")]
TIER_LABELS = {"A": "A - strong", "B": "B - likely", "C": "C - possible", "D": "D - weak"}


def normalize(text):
    return re.sub(r"[^a-z0-9 ]+", "", (text or "").lower().replace("&", " and ")).strip()


def compact(text):
    return normalize(text).replace(" ", "")


def _contains_term(haystack, term, whole=False):
    """Match at a word start so 'ups' never matches 'supplies'.

    Prefix matches are allowed by default ('recycl' matches 'recycling');
    whole=True also requires a word end ('ups' does not match 'upscale').
    """
    term = normalize(term)
    if not term:
        return False
    end = r"(?![a-z0-9])" if whole else ""
    return re.search(rf"(?<![a-z0-9]){re.escape(term)}{end}", haystack) is not None


def _brand_text(lead):
    extra = [c.split("=", 1)[1] for c in lead.raw_categories
             if c.startswith(("brand=", "operator="))]
    return normalize(" ".join([lead.name] + extra))


def _osm_tag_matches(lead, key, value):
    for c in lead.raw_categories:
        k, _, v = c.partition("=")
        if k == key and (value is None or value in v.split(";")):
            return True
    return False


def classify(lead):
    """Return (best category, list of matched categories)."""
    name = normalize(lead.name)
    types = set(lead.raw_categories)
    matched = []
    for cat in config.CATEGORIES:
        hit = None
        if types.intersection(cat.google_types):
            hit = "Google category"
        elif any(_osm_tag_matches(lead, k, v) for k, v in cat.osm_tags):
            hit = "map tag"
        elif any(_contains_term(name, kw) for kw in cat.name_keywords):
            hit = "name"
        if hit:
            matched.append((cat, hit))
    if not matched:
        return None, []
    # Equipment/hauler names win outright: a "compactor" company is not a prospect.
    for cat, hit in matched:
        if cat.key == "equipment" and hit == "name":
            return (cat, hit), matched
    best = max(matched, key=lambda m: m[0].weight)
    return best, matched


def _competitor(lead):
    hay = compact(lead.name) + " " + compact(lead.website)
    for name, aliases in config.COMPETITORS.items():
        if any(compact(a) in hay for a in aliases):
            return name
    return None


def _is_self(lead):
    hay = compact(lead.name) + " " + compact(lead.website)
    return any(compact(a) in hay for a in config.SELF_ALIASES)


def score_lead(lead: Lead, keywords=()):
    reasons, flags = [], []
    best, matched = classify(lead)
    score = 0
    if best:
        cat, how = best
        lead.category = cat.label
        lead.category_key = cat.key
        lead.lead_type = cat.lead_type
        score += cat.weight
        reasons.append(f"+{cat.weight} {cat.label} (by {how}): {cat.why}")
    else:
        lead.category = lead.primary_category or "Uncategorized"
        lead.category_key = ""
        reasons.append("+0 category not on the high-volume list")

    brand_text = _brand_text(lead)
    brand = next((b for b in config.HIGH_VOLUME_BRANDS if _contains_term(brand_text, b, whole=True)), None)
    if brand:
        score += 20
        reasons.append(f"+20 known high-volume brand ({brand})")

    if lead.rating_count:
        if lead.rating_count >= 2000:
            bonus = 15
        elif lead.rating_count >= 500:
            bonus = 10
        elif lead.rating_count >= 100:
            bonus = 5
        else:
            bonus = 0
        if bonus:
            score += bonus
            reasons.append(f"+{bonus} busy site ({lead.rating_count:,} Google reviews)")

    if lead.footprint_sqft:
        if lead.footprint_sqft >= 100_000:
            bonus = 15
        elif lead.footprint_sqft >= 40_000:
            bonus = 10
        elif lead.footprint_sqft >= 15_000:
            bonus = 5
        else:
            bonus = 0
        if bonus:
            score += bonus
            reasons.append(f"+{bonus} large footprint (~{lead.footprint_sqft:,} sq ft)")

    hay = normalize(" ".join([lead.name, lead.primary_category, lead.website]
                             + [c.replace("_", " ").replace("=", " ") for c in lead.raw_categories]))
    kw_hits = [k for k in keywords if k.strip() and _contains_term(hay, k)]
    if kw_hits:
        bonus = min(10 * len(kw_hits), 20)
        score += bonus
        reasons.append(f"+{bonus} matches keyword(s): {', '.join(kw_hits)}")
    lead.matched_keywords = kw_hits

    if len(matched) > 1:
        others = [c.label for c, _ in matched if not best or c.key != best[0].key]
        if others:
            reasons.append("also looks like: " + "; ".join(others))

    competitor = _competitor(lead)
    if competitor:
        lead.lead_type = "Competitor"
        flags.append(f"COMPETITOR: {competitor}")
    elif _is_self(lead):
        lead.lead_type = "Own company"
        flags.append(f"OWN COMPANY: {config.OWN_COMPANY}")
    elif lead.lead_type.startswith("Industry"):
        flags.append("Possible competitor or partner (equipment / hauler)")

    if lead.business_status in ("CLOSED_TEMPORARILY",):
        flags.append("Temporarily closed")
        score -= 10
        reasons.append("-10 temporarily closed")

    lead.score = max(0, min(100, score))
    lead.tier = next(t for threshold, t in TIERS if lead.score >= threshold)
    lead.reasons = reasons
    lead.flags = flags
    return lead
