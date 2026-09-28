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
    s = re.sub(r"[^a-z0-9 ]+", "", (text or "").lower().replace("&", " and "))
    return re.sub(r"\s+", " ", s).strip()


def compact(text):
    return normalize(text).replace(" ", "")


def _contains_term(haystack, term, whole=False):
    """Match at a word start so 'ups' never matches 'supplies'.

    Prefix matches are allowed by default (user keyword 'recycl' matches
    'recycling'); whole=True also requires a word end ('mall' does not match
    'Mallard').
    """
    term = normalize(term)
    if not term:
        return False
    end = r"(?![a-z0-9])" if whole else ""
    return re.search(rf"(?<![a-z0-9]){re.escape(term)}{end}", haystack) is not None


def _brand_tag_text(lead):
    return normalize(" ".join(c.split("=", 1)[1] for c in lead.raw_categories
                              if c.startswith(("brand=", "operator="))))


def _osm_tag_matches(lead, key, value):
    for c in lead.raw_categories:
        k, _, v = c.partition("=")
        if k == key and (value is None or value in v.split(";")):
            return True
    return False


def _only_generic_tags(lead, cat):
    """True if the category matched only through catch-all tags like industrial=*."""
    if lead.source == "google" and set(lead.raw_categories) & set(cat.google_types):
        return False
    specific = [(k, v) for k, v in cat.osm_tags if (k, v) not in config.GENERIC_OSM_TAGS]
    return not any(_osm_tag_matches(lead, k, v) for k, v in specific)


def _is_non_prospect(lead, types):
    return bool(types & config.NON_PROSPECT_GOOGLE_TYPES) or any(
        _osm_tag_matches(lead, k, v) for k, v in config.NON_PROSPECT_OSM_TAGS)


def classify(lead):
    """Return (best category, list of matched categories) as (Category, how) pairs.

    Order of authority: an equipment/hauler name, then what the source says the
    place is (Google type or map tag), then words in the name, then the search
    phrase that found it.
    """
    name = normalize(lead.name)
    types = set(lead.raw_categories)
    matched = []
    for cat in config.CATEGORIES:
        hit = None
        if types.intersection(cat.google_types):
            hit = "Google category"
        elif any(_osm_tag_matches(lead, k, v) for k, v in cat.osm_tags):
            hit = "map tag"
        elif any(_contains_term(name, kw, whole=True) for kw in cat.name_keywords):
            hit = "name"
        elif any(config.QUERY_CATEGORY.get(t) == cat.key for t in lead.search_terms):
            hit = "search query"
        if hit:
            matched.append((cat, hit))

    # Equipment/hauler names win outright: a "compactor" company is not a prospect.
    for cat, hit in matched:
        if cat.key == "equipment" and hit == "name":
            return (cat, hit), matched

    tagged = [m for m in matched if m[1] in ("Google category", "map tag")]
    by_name = [m for m in matched if m[1] == "name"]
    if tagged:
        # A catch-all industrial tag says little; a telling name ("... Waste
        # Management District") may say more.
        if all(_only_generic_tags(lead, m[0]) for m in tagged) and by_name:
            return max(tagged + by_name, key=lambda m: m[0].weight), matched
        return max(tagged, key=lambda m: m[0].weight), matched

    # The place's own tag says it is a vet, clinic, park, gas station...: its
    # name ("Animal Hospital", "University Parking") does not make it a prospect.
    if _is_non_prospect(lead, types):
        return None, matched

    # Any other shop is retail, whatever its name says ("Sportsman's Warehouse").
    if any(c.startswith("shop=") for c in lead.raw_categories) or "store" in types:
        retail = (config.CATEGORY_BY_KEY["retail"],
                  "map tag" if lead.source == "osm" else "Google category")
        return retail, matched + [retail]

    if by_name:
        return max(by_name, key=lambda m: m[0].weight), matched
    if matched:
        return max(matched, key=lambda m: m[0].weight), matched
    return None, []


def _brand_bonus(lead):
    """Return the matched high-volume brand, or None."""
    name = normalize(lead.name)
    if any(_contains_term(name, ex, whole=True) for ex in config.BRAND_EXCLUDE):
        return None
    tag_text = _brand_tag_text(lead)
    text = f"{name} {tag_text}".strip()
    for brand in config.HIGH_VOLUME_BRANDS:
        if _contains_term(text, brand, whole=True):
            return brand
    site = compact(lead.website)
    for brand, cats in config.AMBIGUOUS_BRANDS.items():
        if not _contains_term(text, brand, whole=True):
            continue
        if (_contains_term(tag_text, brand, whole=True) or (site and compact(brand) in site)
                or lead.category_key in cats):
            return brand
    return None


def _all_names(lead):
    return [lead.name, lead.website] + list(lead.alt_names)


def _competitor(lead):
    hay = " ".join(compact(x) for x in _all_names(lead))
    for name, aliases in config.COMPETITORS.items():
        if any(compact(a) in hay for a in aliases):
            return name
    return None


def _is_self(lead):
    hay = " ".join(compact(x) for x in _all_names(lead))
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

    # A brand's helipad, pharmacy or parking lot is not the store.
    blocked = best is None and _is_non_prospect(lead, set(lead.raw_categories))
    brand = None if blocked else _brand_bonus(lead)
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
