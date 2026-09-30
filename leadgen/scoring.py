"""Relevance scoring: how likely is this location to run a large compactor or baler?

Score (0-100) = category weight + brand bonus + size bonus (reviews, footprint) + keyword bonus.
Every point is explained in `lead.reasons` so the client can see why a lead
ranked where it did and tell us which rules to adjust.
"""

import re
from collections.abc import Iterable
from functools import lru_cache

from . import config
from .models import Lead

# A matched category and how it matched ("Google category", "name", ...).
Match = tuple[config.Category, str]

TIERS = [(60, "A"), (40, "B"), (20, "C"), (0, "D")]
TIER_LABELS = {"A": "A - strong", "B": "B - likely", "C": "C - possible", "D": "D - weak"}


@lru_cache(maxsize=65536)
def normalize(text: str) -> str:
    s = re.sub(r"[^a-z0-9 ]+", "", (text or "").lower().replace("&", " and "))
    return re.sub(r"\s+", " ", s).strip()


def compact(text: str) -> str:
    return normalize(text).replace(" ", "")


@lru_cache(maxsize=4096)
def _term_pattern(term: str, whole: bool) -> re.Pattern[str]:
    end = r"(?![a-z0-9])" if whole else ""
    return re.compile(rf"(?<![a-z0-9]){re.escape(term)}{end}")


def _contains_term(haystack: str, term: str, whole: bool = False) -> bool:
    """Match at a word start so 'ups' never matches 'supplies'.

    Prefix matches are allowed by default (user keyword 'recycl' matches
    'recycling'); whole=True also requires a word end ('mall' does not match
    'Mallard').
    """
    term = normalize(term)
    if not term:
        return False
    return _term_pattern(term, whole).search(haystack) is not None


def yelp_categories(lead: Lead) -> set[str]:
    """Yelp category aliases on a lead (stored as "yelp:alias")."""
    return {c[5:] for c in lead.raw_categories if c.startswith("yelp:")}


def google_types(lead: Lead) -> set[str]:
    """Google types on a lead: anything that is not an OSM "k=v" tag or a Yelp category."""
    return {c for c in lead.raw_categories if "=" not in c and not c.startswith("yelp:")}


def _osm_tag_matches(lead: Lead, key: str, value: str | None) -> bool:
    for c in lead.raw_categories:
        k, _, v = c.partition("=")
        if k == key and (value is None or value in v.split(";")):
            return True
    return False


def _only_generic_tags(lead: Lead, cat: config.Category) -> bool:
    """True if the category matched only through catch-all tags like industrial=*."""
    if set(lead.raw_categories) & set(cat.google_types):
        return False
    if yelp_categories(lead) & set(cat.yelp_categories):
        return False
    specific = [(k, v) for k, v in cat.osm_tags if (k, v) not in config.GENERIC_OSM_TAGS]
    return not any(_osm_tag_matches(lead, k, v) for k, v in specific)


def _is_non_prospect(lead: Lead, types: set[str]) -> bool:
    return (bool(types & config.NON_PROSPECT_GOOGLE_TYPES)
            or bool(yelp_categories(lead) & config.NON_PROSPECT_YELP_CATEGORIES)
            or any(_osm_tag_matches(lead, k, v) for k, v in config.NON_PROSPECT_OSM_TAGS))


def is_non_prospect_tag(tag: str) -> bool:
    """True for a single Google type, Yelp category or OSM tag that marks a non-prospect place."""
    if tag in config.NON_PROSPECT_GOOGLE_TYPES:
        return True
    if tag.startswith("yelp:"):
        return tag[5:] in config.NON_PROSPECT_YELP_CATEGORIES
    k, sep, v = tag.partition("=")
    return bool(sep) and any(k == nk and (nv is None or nv in v.split(";"))
                             for nk, nv in config.NON_PROSPECT_OSM_TAGS)


def _not_campus(name: str) -> bool:
    """True for a name that is a campus's unit or neighbour, not the campus (config.NOT_CAMPUS_WORDS)."""
    if any(_contains_term(name, w, whole=True) for w in config.NOT_CAMPUS_WORDS):
        return True
    return any(m.start() > 0 for w in config.NOT_CAMPUS_INNER
               for m in _term_pattern(normalize(w), True).finditer(name))


def non_prospect_name(lead: Lead) -> bool:
    """True when the name says the place is not a prospect (config.NON_PROSPECT_NAME_WORDS:
    police, fire, impound, trailer yards, parcel lockers...), unless a specific tag says
    it is a recycling site or transfer station."""
    name = normalize(lead.name)
    if not any(_contains_term(name, w, whole=True) for w in config.NON_PROSPECT_NAME_WORDS):
        return False
    rescued = (any(_osm_tag_matches(lead, k, v) for k, v in config.NAME_BLOCK_RESCUE_TAGS)
               or bool(google_types(lead) & config.NAME_BLOCK_RESCUE_GOOGLE_TYPES))
    return not rescued


def utility_structure(lead: Lead) -> bool:
    """True for a pumping station, water well, substation or similar (config.UTILITY_OSM_TAGS),
    unless a specific tag says it is a recycling site or transfer station."""
    if not any(_osm_tag_matches(lead, k, v) for k, v in config.UTILITY_OSM_TAGS):
        return False
    return not any(_osm_tag_matches(lead, k, v) for k, v in config.NAME_BLOCK_RESCUE_TAGS)


def _utility_named(lead: Lead) -> bool:
    """True when the name or operator tag says a catch-all industrial building belongs to
    a utility, a city or a transit agency (config.UTILITY_NAME_WORDS / _OPERATOR_WORDS)."""
    name = normalize(lead.name)
    if any(_contains_term(name, w, whole=True) for w in config.UTILITY_NAME_WORDS):
        return True
    operators = normalize(" ".join(c.split("=", 1)[1] for c in lead.raw_categories
                                   if c.startswith("operator=")))
    return any(_contains_term(operators, w, whole=True) for w in config.UTILITY_OPERATOR_WORDS)


def _small_generic_building(lead: Lead) -> bool:
    """True for a building known only as building=industrial / landuse=industrial (no
    industrial=* use tag) whose footprint is under config.SMALL_GENERIC_BUILDING_SQFT."""
    if not lead.footprint_sqft or lead.footprint_sqft >= config.SMALL_GENERIC_BUILDING_SQFT:
        return False
    return not any(c.startswith(("industrial=", "man_made=works")) for c in lead.raw_categories)


def _shop_named(lead: Lead, name: str) -> bool:
    """True when the name or the map says the place is a shop (config.NOT_MANUFACTURING_NAME_WORDS,
    config.RETAIL_OSM_TAGS), so a manufacturing word in its name says nothing."""
    return (any(_contains_term(name, w, whole=True) for w in config.NOT_MANUFACTURING_NAME_WORDS)
            or any(_osm_tag_matches(lead, k, v) for k, v in config.RETAIL_OSM_TAGS))


def self_storage(lead: Lead) -> bool:
    """True for a self-storage place (units rented to the public), by its tag, Google
    type or name (config.SELF_STORAGE_*), unless a specific tag says it is a recycling
    site or transfer station. A cold store or a logistics firm's storage is not."""
    if any(_osm_tag_matches(lead, k, v) for k, v in config.NAME_BLOCK_RESCUE_TAGS):
        return False
    if (any(_osm_tag_matches(lead, k, v) for k, v in config.SELF_STORAGE_OSM_TAGS)
            or google_types(lead) & config.SELF_STORAGE_GOOGLE_TYPES):
        return True
    name = normalize(lead.name)
    if any(_contains_term(name, w, whole=True) for w in config.SELF_STORAGE_NAME_WORDS):
        return True
    return (_contains_term(name, "storage", whole=True)
            and not any(_contains_term(name, w, whole=True)
                        for w in config.COLD_STORAGE_WORDS + config.LOGISTICS_NAME_WORDS))


def _not_plant_named(name: str) -> bool:
    """True when the name says a catch-all industrial building is a data centre, a career
    centre or a city's shops (config.NOT_PLANT_NAME_WORDS)."""
    return any(_contains_term(name, w, whole=True) for w in config.NOT_PLANT_NAME_WORDS)


def _retail_brand(lead: Lead) -> config.Category | None:
    """The retail category of a chain whose name holds a warehouse word ("Harbor
    Freight"; config.RETAIL_NAME_BRANDS), from its name or its map brand tag."""
    brand_tags = normalize(" ".join(c.split("=", 1)[1] for c in lead.raw_categories
                                    if c.startswith("brand=")))
    for text in (normalize(lead.name), brand_tags):
        for brand, key in config.RETAIL_NAME_BRANDS.items():
            if _contains_term(text, brand, whole=True):
                return config.CATEGORY_BY_KEY[key]
    return None


def _shop_mapped_as_mall(lead: Lead, name: str) -> bool:
    """True when a shop is mapped as a mall (a furniture store tagged shop=mall): the
    only venue tag is shop=mall, its name says shop (or it has another shop tag) and
    nothing in the name says shopping centre (config.MALL_NAME_WORDS)."""
    venue = config.CATEGORY_BY_KEY["venue"]
    tags = [(k, v) for k, v in venue.osm_tags if _osm_tag_matches(lead, k, v)]
    if tags != [("shop", "mall")]:
        return False
    if any(_contains_term(name, w, whole=True) for w in config.MALL_NAME_WORDS):
        return False
    other_shop = any(c.startswith("shop=") and v != "mall"
                     for c in lead.raw_categories for v in c.split("=", 1)[1].split(";"))
    return other_shop or any(_contains_term(name, w, whole=True) for w in config.SHOP_NAME_WORDS)


def generic_name(name: str) -> bool:
    """True when a name is only a generic word ("Recycling", "Junkyard"), alone or made
    descriptive only by its street or city ("Recycling at 1200 W 500 S")."""
    base = re.split(r"\s+(?:at|near)\s+|,", name.strip(), maxsplit=1)[0]
    return normalize(base) in {normalize(g) for g in config.GENERIC_NAMES}


def classify(lead: Lead) -> tuple[Match | None, list[Match]]:
    """Return (best category, list of matched categories) as (Category, how) pairs.

    Order of authority: an equipment/hauler name; a specific Google type, Yelp
    category or map tag; a non-prospect type/tag (vet, clinic, gas station: no
    category); the retail fallback for shops; a catch-all industrial tag or
    words in the name; and last, the search phrase that found a generically
    typed Google result.
    """
    name = normalize(lead.name)
    types = set(lead.raw_categories)
    gtypes = google_types(lead)
    yelp = yelp_categories(lead)
    use_hint = not (gtypes - config.GENERIC_GOOGLE_TYPES) and not yelp
    retail_brand = _retail_brand(lead)
    matched = []
    for cat in config.CATEGORIES:
        hit = None
        if gtypes.intersection(cat.google_types):
            hit = "Google category"
        elif yelp.intersection(cat.yelp_categories):
            hit = "Yelp category"
        elif any(_osm_tag_matches(lead, k, v) for k, v in cat.osm_tags):
            hit = "map tag"
        elif any(_contains_term(name, kw, whole=True) for kw in cat.name_keywords):
            hit = "name"
        elif use_hint and any(config.QUERY_CATEGORY.get(t) == cat.key for t in lead.search_terms):
            hit = "search query"
        if hit and cat.key == "education" and _not_campus(name):
            hit = None          # "University Heights Condominiums", "University of Utah Press"
        if hit == "name" and cat.key == "food_production" and any(
                _contains_term(name, w, whole=True) for w in config.NOT_PRODUCTION_NAME_WORDS):
            hit = None          # "Day Dairy Barn" is a small shop, not a plant
        if hit == "name" and cat.key == "manufacturing" and _shop_named(lead, name):
            hit = None          # "Deseret Industries Thrift Store" is a shop, not a plant
        if hit == "map tag" and cat.key == "venue" and _shop_mapped_as_mall(lead, name):
            hit = None          # "Liddiard Furniture" mapped as a mall is a furniture shop
        if hit == "name" and cat.key in ("distribution", "manufacturing") and retail_brand:
            hit = None          # "Harbor Freight" sells tools: "freight" says nothing
        if hit:
            matched.append((cat, hit))

    # Equipment/hauler names win outright: a "compactor" company is not a prospect.
    for cat, hit in matched:
        if cat.key == "equipment" and hit == "name":
            return (cat, hit), matched

    # A police impound lot, a fire department's logistics centre, a trailer yard or a
    # parcel-locker brand: no prospect category, whatever its tags or other words say.
    if non_prospect_name(lead):
        return None, matched
    # A pumping station, water well or substation has no waste stream to compact.
    if utility_structure(lead):
        return None, matched
    # Self-storage units: the tenants take their rubbish home.
    if self_storage(lead):
        return None, matched

    def best_of(options: list[Match]) -> Match:
        return max(options, key=lambda m: m[0].weight)

    tagged = [m for m in matched if m[1] in ("Google category", "Yelp category", "map tag")]
    by_name = [m for m in matched if m[1] == "name"]
    # A catch-all industrial tag describes the building, not the business.
    specific = [m for m in tagged if not _only_generic_tags(lead, m[0])]
    if retail_brand:
        # A retail chain's name decides over a warehouse building it is mapped as (which
        # then isn't offered as "also looks like" either).
        matched = [m for m in matched if m[0].key not in ("distribution", "manufacturing")]
        specific = [m for m in specific if m in matched]
        if not specific:
            brand = (retail_brand, "retail brand name")
            return brand, matched + [brand]
    if specific:
        best = best_of(specific)
        # Google and Yelp label production breweries/bakeries like taprooms and cafes.
        production = (gtypes & config.PRODUCTION_GOOGLE_TYPES) or (yelp & config.YELP_PRODUCTION)
        service = (gtypes & config.SERVICE_GOOGLE_TYPES) or (yelp & config.YELP_SERVICE)
        if best[0].key == "food_service" and production and not service:
            prod = [m for m in matched if m[0].key == "food_production"]
            if prod:
                return prod[0], matched
        return best, matched

    # The place's own tag says it is a vet, clinic, park, gas station...: its
    # name ("Animal Hospital", "University Parking") does not make it a prospect.
    if _is_non_prospect(lead, types):
        blockers = gtypes & config.NON_PROSPECT_GOOGLE_TYPES
        allowed = (set.intersection(*(config.NAME_BEATS_GOOGLE_TYPE.get(t, set())
                                      for t in blockers)) if blockers else set())
        other_blocked = (bool(yelp & config.NON_PROSPECT_YELP_CATEGORIES) or any(
            _osm_tag_matches(lead, k, v) for k, v in config.NON_PROSPECT_OSM_TAGS))
        rescued = [m for m in by_name if m[0].key in allowed]
        if rescued and not other_blocked:
            return best_of(rescued), matched
        return None, matched

    # Any other shop is retail, whatever its name says ("Sportsman's Warehouse").
    if any(c.startswith("shop=") for c in lead.raw_categories) or "store" in types:
        retail = (config.CATEGORY_BY_KEY["retail"],
                  "Google category" if "store" in gtypes else "map tag")
        return retail, matched + [retail]

    telling_name = by_name or any(_contains_term(name, kw, whole=True)
                                  for cat, _ in tagged for kw in cat.name_keywords)
    if tagged and not telling_name and (_small_generic_building(lead) or _utility_named(lead)
                                        or _not_plant_named(name)):
        # Only a catch-all industrial tag on a shed-sized building, on a city's or a
        # utility's structure ("Pacificorp", 900 sq ft), a data centre or a career
        # centre: not a plant.
        return None, matched
    if tagged or by_name:
        # Only catch-all tags; a telling name ("... Waste Management District") may say more.
        return best_of(tagged + by_name), matched
    if matched:
        return best_of(matched), matched
    return None, []


def _split_hyphens(text: str) -> str:
    return normalize(re.sub(r"[-/]", " ", text or ""))


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


def _all_names(lead: Lead) -> list[str]:
    return [lead.name, lead.website] + list(lead.alt_names)


def _competitor(lead: Lead) -> str | None:
    hay = " ".join(compact(x) for x in _all_names(lead))
    for name, aliases in config.COMPETITORS.items():
        if any(compact(a) in hay for a in aliases):
            return name
    return None


def _is_self(lead: Lead) -> bool:
    hay = " ".join(compact(x) for x in _all_names(lead))
    return any(compact(a) in hay for a in config.SELF_ALIASES)


def keyword_hits(lead: Lead, keywords: Iterable[str]) -> list[str]:
    """The words (of keywords) found in the lead's name, category, website or tags."""
    hay = normalize(" ".join([lead.name, lead.primary_category, lead.website]
                             + [c.removeprefix("yelp:").replace("_", " ").replace("=", " ")
                                for c in lead.raw_categories]))
    return [k for k in keywords if k.strip() and _contains_term(hay, k)]


def score_lead(lead: Lead, keywords: Iterable[str] = ()) -> Lead:
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
    blocked = best is None and (_is_non_prospect(lead, set(lead.raw_categories))
                                or non_prospect_name(lead) or utility_structure(lead)
                                or self_storage(lead))
    brand = None if blocked else _brand_bonus(lead)
    if brand:
        score += 20
        reasons.append(f"+20 known high-volume brand ({brand})")

    # Busy site: the stronger of the Google and Yelp review signals (never both).
    busy = max(((pts, count, src) for src, count in (("Google", lead.rating_count),
                                                      ("Yelp", lead.yelp_reviews)) if count
                for threshold, pts in config.REVIEW_BONUS[src]
                if count >= threshold), default=None)
    if busy:
        bonus, count, src = busy
        score += bonus
        reasons.append(f"+{bonus} busy site ({count:,} {src} reviews)")

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

    kw_hits = keyword_hits(lead, keywords)
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

    if generic_name(lead.name):
        # "Recycling" with no operator: nothing to look up or call; named places come first.
        score -= config.GENERIC_NAME_PENALTY
        reasons.append(f"-{config.GENERIC_NAME_PENALTY} no business name on the map "
                       f"(only \"{lead.name}\")")

    if lead.business_status in ("CLOSED_TEMPORARILY",):
        flags.append("Temporarily closed")
        score -= 10
        reasons.append("-10 temporarily closed")

    lead.score = max(0, min(100, score))
    lead.tier = next(t for threshold, t in TIERS if lead.score >= threshold)
    lead.reasons = reasons
    lead.flags = flags
    return lead
