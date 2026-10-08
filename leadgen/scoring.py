"""Relevance scoring: how likely is this location to run a large compactor or baler?

Score (0-100) = category weight + brand bonus + size bonus (reviews, footprint) + keyword bonus.
Every point is explained in `lead.reasons` so the client can see why a lead
ranked where it did and tell us which rules to adjust.

The parts live beside this module, and every name they had here is still imported from
here: scoring_categories.py (classifying a lead: RULES, VETOES), scoring_names.py (what
a listing's name and tags say it is), scoring_flags.py (AARCO's own, its parent
company's and the competitors' listings), scoring_explain.py (the explanation as a
salesperson reads it) and scoring_text.py (the text helpers they share).
"""

import re
from collections.abc import Iterable

from . import config
from .models import Lead
from .scoring_categories import (
    GOOGLE,
    NAME,
    NO_MATCH,
    QUERY,
    RETAIL_BRAND,
    RULES,
    TAG,
    TAGGED,
    VETOES,
    YELP,
    Classified,
    Decision,
    Facts,
    Match,
    Rule,
    Veto,
    classify,
)
from .scoring_explain import PLAIN_HOW, plain_reasons
from .scoring_flags import (
    COMPETITOR_FLAG_START,
    INDUSTRY_FLAG,
    OWN_FLAG,
    OWN_FLAG_START,
    OWN_TYPE,
    PARENT_FLAG,
    PROSPECT_TYPE,
    _type_flags,
    current_own_flag,
    current_type_flags,
    mark_parent_company,
    parent_company,
)
from .scoring_names import (
    _building_name,
    _is_non_prospect,
    clinic,
    generic_name,
    is_non_prospect_tag,
    needs_name,
    non_prospect_name,
    parcel_station,
    self_storage,
    service_plant,
    utility_structure,
    vehicle_wash,
)
from .scoring_text import (
    _contains_term,
    _split_hyphens,
    _term_pattern,
    compact,
    google_types,
    normalize,
    yelp_categories,
)

# Re-exported: callers import these from scoring, wherever they are written.
__all__ = ["COMPETITOR_FLAG_START", "GOOGLE", "INDUSTRY_FLAG", "NAME", "NO_MATCH", "OWN_FLAG", "OWN_FLAG_START",
           "OWN_TYPE", "PARENT_FLAG", "PLAIN_HOW", "PROSPECT_TYPE", "QUERY", "RETAIL_BRAND", "RULES", "TAG",
           "TAGGED", "VETOES", "YELP", "Classified", "Decision", "Facts", "Match", "Rule", "Veto", "classify",
           "clinic", "compact", "current_own_flag", "current_type_flags", "generic_name", "google_types",
           "is_non_prospect_tag", "mark_parent_company", "needs_name", "non_prospect_name", "normalize",
           "parcel_station", "parent_company", "plain_reasons", "self_storage", "service_plant",
           "utility_structure", "vehicle_wash", "yelp_categories"]

TIERS = [(60, "A"), (40, "B"), (20, "C"), (0, "D")]
TIER_LABELS = {"A": "A - strong", "B": "B - likely", "C": "C - possible", "D": "D - weak"}


def tier_of(score: int) -> str:
    return next(t for threshold, t in TIERS if score >= threshold)


def mark_needs_name(lead: Lead) -> Lead:
    """A building with no business name, address or phone (needs_name) is no lead to call:
    it is held at config.NEEDS_NAME_MAX_SCORE (tier D, below the default minimum score,
    so a search leaves it out) and flagged config.NEEDS_NAME_FLAG. score_lead does it,
    and so does showing a row saved before (saved.py; the saved row is unchanged). Done
    once: a lead already flagged is left as it is."""
    if config.NEEDS_NAME_FLAG in lead.flags or not needs_name(lead):
        return lead
    what = "only describes a building" if _building_name(lead.name) else "is only a word for a kind of place"
    said = f"no business name, address or phone on the map: \"{lead.name}\" {what}"
    cut = max(0, lead.score - config.NEEDS_NAME_MAX_SCORE)
    lead.reasons = [*lead.reasons, f"-{cut} {said}" if cut else said]
    lead.score -= cut
    lead.tier = tier_of(lead.score)
    lead.flags = [*lead.flags, config.NEEDS_NAME_FLAG]
    return lead


def _name_is_brand(raw_name: str, brand: str, own_brand: str) -> bool:
    """True when a name that contains `brand` is that brand's business, not another
    business named after it: its own map brand tag is not a different brand ("Tru", for
    "Tru by Hilton Clearfield Hill Air Force Base"), a place-name brand opens the name,
    and no location word comes just before it ("Hotel near Costco")."""
    if own_brand and not _contains_term(normalize(own_brand), brand, whole=True):
        return False
    for name in (normalize(raw_name), _split_hyphens(raw_name)):
        pattern = _term_pattern(normalize(brand), True)
        for m in pattern.finditer(name):
            before = name[:m.start()].strip()
            if brand in config.PLACE_NAME_BRANDS and before:
                continue
            if any(before == w or before.endswith(" " + w) for w in config.BRAND_LOCATION_WORDS):
                continue
            return True
    return False


def _brand_bonus(lead: Lead) -> str | None:
    """Return the matched high-volume brand, or None."""
    name = normalize(lead.name)
    if any(_contains_term(name, ex, whole=True) for ex in config.BRAND_EXCLUDE):
        return None
    tag_raw = " ".join(c.split("=", 1)[1] for c in lead.raw_categories
                       if c.startswith(("brand=", "operator=")))
    tag_text = f"{normalize(tag_raw)} {_split_hyphens(tag_raw)}".strip()
    own_brand = " ".join(c.split("=", 1)[1] for c in lead.raw_categories if c.startswith("brand="))
    # Match both "Pepsi-Cola" -> "pepsicola" and "pepsi cola".
    names = f"{name} {_split_hyphens(lead.name)}"
    text = f"{names} {tag_text}".strip()
    for brand in config.HIGH_VOLUME_BRANDS:
        if _contains_term(tag_text, brand, whole=True):
            return brand
        if _contains_term(names, brand, whole=True) and _name_is_brand(lead.name, brand, own_brand):
            return brand
    host = re.sub(r"^(https?://)?(www\d?\.)?", "", (lead.website or "").lower()).split("/")[0]
    site_label = compact(host.split(".")[0]) if host else ""
    for brand, cats in config.AMBIGUOUS_BRANDS.items():
        if not _contains_term(text, brand, whole=True):
            continue
        if _contains_term(tag_text, brand, whole=True) or (site_label and site_label == compact(brand)):
            return brand
        if lead.category_key in cats:
            rest = re.sub(r"\b(store|stores|home improvement|\d+)\b", "",
                          name.replace(normalize(brand), "", 1)).strip()
            if not rest or lead.category_key not in ("retail", "specialty_retail"):
                return brand
    return None


def keyword_hits(lead: Lead, keywords: Iterable[str]) -> list[str]:
    """The words (of keywords) found in the lead's name, category, website or tags."""
    hay = normalize(" ".join([lead.name, lead.primary_category, lead.website]
                             + [c.removeprefix("yelp:").replace("_", " ").replace("=", " ")
                                for c in lead.raw_categories]))
    return [k for k in keywords if k.strip() and _contains_term(hay, k)]


# Points a lead earned (or lost) and the reasons that say why, as each part of score_lead adds them.
Points = tuple[int, list[str]]


def _category_points(lead: Lead, best: Match | None, rule: Rule) -> Points:
    """The category's weight, with the lead's category and type set from it (or from the
    listing's own category when none matched), and which rule decided."""
    reasons = []
    if best:
        cat, how = best
        lead.category = cat.label
        lead.category_key = cat.key
        lead.lead_type = cat.lead_type
        reasons.append(f"+{cat.weight} {cat.label} (by {how}): {cat.why}")
    else:
        lead.category = lead.primary_category or "Uncategorized"
        lead.category_key = ""
        # Not the type an earlier scoring gave a saved listing (saved._rebuild).
        lead.lead_type = PROSPECT_TYPE
        reasons.append("+0 category not on the high-volume list")
    if rule is not NO_MATCH:
        # Which classifying rule decided (RULES), for anyone checking why a lead scored as it did.
        reasons.append(f"category rule ({rule.name}): {rule.says}")
    return (best[0].weight if best else 0), reasons


def _brand_points(lead: Lead, best: Match | None) -> Points:
    """+20 for a known high-volume brand's store (config.BRANDS), unless the listing is
    one of its structures."""
    # A brand's helipad, pharmacy or parking lot is not the store.
    blocked = best is None and (_is_non_prospect(lead, set(lead.raw_categories))
                                or non_prospect_name(lead) or utility_structure(lead)
                                or self_storage(lead) or service_plant(lead) or vehicle_wash(lead)
                                or clinic(lead))
    brand = None if blocked else _brand_bonus(lead)
    if not brand:
        return 0, []
    return 20, [f"+20 known high-volume brand ({config.brand_name(brand)})"]


def _busy_points(lead: Lead) -> Points:
    """The review-count bonus (config.REVIEW_BONUS) for a busy site."""
    # Busy site: the stronger of the Google and Yelp review signals (never both).
    busy = max(((pts, count, src) for src, count in (("Google", lead.rating_count),
                                                      ("Yelp", lead.yelp_reviews)) if count
                for threshold, pts in config.REVIEW_BONUS[src]
                if count >= threshold), default=None)
    if not busy:
        return 0, []
    bonus, count, src = busy
    return bonus, [f"+{bonus} busy site ({count:,} {src} reviews)"]


def _footprint_points(lead: Lead) -> Points:
    """The bonus for a large building (a map outline's area)."""
    if not lead.footprint_sqft:
        return 0, []
    if lead.footprint_sqft >= 100_000:
        bonus = 15
    elif lead.footprint_sqft >= 40_000:
        bonus = 10
    elif lead.footprint_sqft >= 15_000:
        bonus = 5
    else:
        return 0, []
    return bonus, [f"+{bonus} large footprint (~{lead.footprint_sqft:,} sq ft)"]


def _keyword_points(lead: Lead, keywords: Iterable[str]) -> Points:
    """The bonus for the search's keywords found on the lead (kept as matched_keywords)."""
    kw_hits = keyword_hits(lead, keywords)
    lead.matched_keywords = kw_hits
    if not kw_hits:
        return 0, []
    bonus = min(10 * len(kw_hits), 20)
    return bonus, [f"+{bonus} matches keyword(s): {', '.join(kw_hits)}"]


def _also_like(best: Match | None, matched: list[Match]) -> list[str]:
    """The other categories the lead also matched, as one reason line."""
    if len(matched) <= 1:
        return []
    others = [c.label for c, _ in matched if not best or c.key != best[0].key]
    return ["also looks like: " + "; ".join(others)] if others else []


def _penalties(lead: Lead, flags: list[str]) -> Points:
    """Points taken off: a map name that is only a word, and a temporary closure (flagged)."""
    points, reasons = 0, []
    if generic_name(lead.name):
        # "Recycling" with no operator: nothing to look up or call; named places come first.
        points -= config.GENERIC_NAME_PENALTY
        reasons.append(f"-{config.GENERIC_NAME_PENALTY} no business name on the map "
                       f"(only \"{lead.name}\")")
    if lead.business_status in ("CLOSED_TEMPORARILY",):
        flags.append("Temporarily closed")
        points -= 10
        reasons.append("-10 temporarily closed")
    return points, reasons


def score_lead(lead: Lead, keywords: Iterable[str] = ()) -> Lead:
    """Score the lead 0-100 and set its category, type, tier, reasons and flags: its
    category's points, the brand, busy-site, footprint and keyword bonuses, then the
    penalties; a competitor or AARCO's own listing is typed and flagged as such."""
    best, matched, rule = classify(lead)
    score, reasons = _category_points(lead, best, rule)
    for points, why in (_brand_points(lead, best), _busy_points(lead), _footprint_points(lead),
                        _keyword_points(lead, keywords)):
        score += points
        reasons += why
    reasons += _also_like(best, matched)
    flags = _type_flags(lead)
    points, why = _penalties(lead, flags)
    score += points
    reasons += why
    lead.score = max(0, min(100, score))
    lead.tier = tier_of(lead.score)
    lead.reasons = reasons
    lead.flags = flags
    return mark_needs_name(lead)
