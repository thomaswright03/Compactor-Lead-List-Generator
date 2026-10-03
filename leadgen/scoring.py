"""Relevance scoring: how likely is this location to run a large compactor or baler?

Score (0-100) = category weight + brand bonus + size bonus (reviews, footprint) + keyword bonus.
Every point is explained in `lead.reasons` so the client can see why a lead
ranked where it did and tell us which rules to adjust.
"""

import functools
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import lru_cache
from typing import NamedTuple

from . import config
from .models import Lead

# A matched category and how it matched ("Google category", "name", ...).
Match = tuple[config.Category, str]

TIERS = [(60, "A"), (40, "B"), (20, "C"), (0, "D")]
TIER_LABELS = {"A": "A - strong", "B": "B - likely", "C": "C - possible", "D": "D - weak"}
# The flag on AARCO's own listing; rows saved under an earlier name show it as it is now
# (saved.py).
OWN_FLAG_START = "OWN COMPANY: "
OWN_FLAG = OWN_FLAG_START + config.OWN_COMPANY


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


def parcel_station(lead: Lead) -> bool:
    """True for a parcel carrier's delivery station or home-delivery hub, by its name and
    its operator / brand tags: a carrier (config.PARCEL_CARRIERS) and a station word
    (config.PARCEL_STATION_WORDS), "FedEx Home Delivery" operated by FedEx, say."""
    tags = " ".join(c.split("=", 1)[1] for c in lead.raw_categories if c.startswith(("operator=", "brand=")))
    text = f"{normalize(lead.name)} {normalize(tags)}"
    return (any(_contains_term(text, c, whole=True) for c in config.PARCEL_CARRIERS)
            and any(_contains_term(text, w, whole=True) for w in config.PARCEL_STATION_WORDS))


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
    other_shop = any(v != "mall" for c in lead.raw_categories if c.startswith("shop=")
                     for v in c.split("=", 1)[1].split(";"))
    return other_shop or any(_contains_term(name, w, whole=True) for w in config.SHOP_NAME_WORDS)


def generic_name(name: str) -> bool:
    """True when a name is only a generic word ("Recycling", "Junkyard"), alone or made
    descriptive only by its street or city ("Recycling at 1200 W 500 S")."""
    base = re.split(r"\s+(?:at|near)\s+|,", name.strip(), maxsplit=1)[0]
    return normalize(base) in {normalize(g) for g in config.GENERIC_NAMES}


# ---- classifying a lead: an ordered list of named rules
#
# A lead's category is decided in two steps, both tables below. First every category
# that matches it is found, the strongest way first (MATCH_ORDER: its Google category,
# Yelp category, map tag, a word of its name, or the search phrase that found a
# generically typed Google result), less the matches a VETOES entry says mean nothing
# ("University Heights Condominiums" is not a campus). Then RULES are tried in order:
# the first that applies decides the category (or that there is none), and the lead's
# explanation names it. To handle a new kind of misclassification, add an entry to the
# table where it belongs and a test with its example (tests/test_classify_rules.py).

# The ways a category can match a lead, strongest first.
GOOGLE, YELP, TAG, NAME, QUERY = "Google category", "Yelp category", "map tag", "name", "search query"
TAGGED = (GOOGLE, YELP, TAG)
# A retail chain's name decided the category (the rule "retail chain").
RETAIL_BRAND = "retail brand name"


class Facts:
    """What the rules look at, worked out once per lead."""

    def __init__(self, lead: Lead) -> None:
        self.lead = lead
        self.name = normalize(lead.name)
        self.types = set(lead.raw_categories)
        self.gtypes = google_types(lead)
        self.yelp = yelp_categories(lead)
        self.retail_brand = _retail_brand(lead)
        # A generically typed Google result: the phrase that found it says what it is.
        self.use_hint = not (self.gtypes - config.GENERIC_GOOGLE_TYPES) and not self.yelp
        # Every category that matched, and how (VETOES applied).
        self.matched: list[Match] = [m for m in map(self._match, config.CATEGORIES) if m]
        self.tagged = [m for m in self.matched if m[1] in TAGGED]
        self.by_name = [m for m in self.matched if m[1] == NAME]
        # A catch-all industrial tag describes the building, not the business.
        specific = [m for m in self.tagged if not _only_generic_tags(lead, m[0])]
        # A retail chain's name decides over a warehouse building it is mapped as (which
        # then isn't offered as "also looks like" either).
        self.kept = ([m for m in self.matched if m[0].key not in ("distribution", "manufacturing")]
                     if self.retail_brand else self.matched)
        self.specific = [m for m in specific if m in self.kept]

    def _match(self, cat: config.Category) -> Match | None:
        how = self._how(cat)
        if how and any(cat.key in v.categories and (not v.hows or how in v.hows) and v.test(self)
                       for v in VETOES):
            return None
        return (cat, how) if how else None

    def _how(self, cat: config.Category) -> str | None:
        if self.gtypes.intersection(cat.google_types):
            return GOOGLE
        if self.yelp.intersection(cat.yelp_categories):
            return YELP
        if any(_osm_tag_matches(self.lead, k, v) for k, v in cat.osm_tags):
            return TAG
        if any(_contains_term(self.name, kw, whole=True) for kw in cat.name_keywords):
            return NAME
        if cat.key == "distribution" and parcel_station(self.lead):
            return NAME                  # a carrier's delivery station (config.PARCEL_CARRIERS)
        if self.use_hint and any(config.QUERY_CATEGORY.get(t) == cat.key for t in self.lead.search_terms):
            return QUERY
        return None

    @functools.cached_property
    def non_prospect_type(self) -> bool:
        """Its own Google type, Yelp category or map tag says it is a vet, clinic, park..."""
        return _is_non_prospect(self.lead, self.types)


@dataclass(frozen=True)
class Veto:
    """A match that says nothing: `categories` matched (in one of `hows`; any way when
    empty), but `test` says the lead is something else (the example beside each)."""

    name: str
    categories: frozenset[str]
    hows: frozenset[str]
    test: Callable[[Facts], bool]


def _words(name: str, words: Iterable[str]) -> bool:
    return any(_contains_term(name, w, whole=True) for w in words)


VETOES = (
    # "University Heights Condominiums", "University of Utah Press"
    Veto("not the campus itself", frozenset({"education"}), frozenset(), lambda f: _not_campus(f.name)),
    # "Day Dairy Barn" is a small shop, not a plant
    Veto("a small food shop", frozenset({"food_production"}), frozenset({NAME}),
         lambda f: _words(f.name, config.NOT_PRODUCTION_NAME_WORDS)),
    # "Deseret Industries Thrift Store" is a shop, not a plant
    Veto("a shop named like a plant", frozenset({"manufacturing"}), frozenset({NAME}),
         lambda f: _shop_named(f.lead, f.name)),
    # "Liddiard Furniture" mapped as a mall (shop=mall) is a furniture shop
    Veto("a shop mapped as a shopping centre", frozenset({"venue"}), frozenset({TAG}),
         lambda f: _shop_mapped_as_mall(f.lead, f.name)),
    # "Harbor Freight" sells tools: "freight" says nothing
    Veto("a retail chain's name", frozenset({"distribution", "manufacturing"}), frozenset({NAME}),
         lambda f: f.retail_brand is not None),
)

# What a rule decides: (the category and how it matched, or None for no category; every
# category that matched, offered as "also looks like").
Decision = tuple[Match | None, list[Match]]


@dataclass(frozen=True)
class Rule:
    """One step of classifying: `decide` gives the decision when the rule applies, else
    None (the next rule is tried). `name` and `says` are for whoever maintains the
    rules (the stored explanation keeps them: "category rule (name): says"); `note` is
    what a salesperson reads about it on the Leads page and in the downloads
    (plain_reasons), or "" when the category's own line says it all."""

    name: str
    says: str
    decide: Callable[[Facts], Decision | None]
    note: str = ""


def _best(options: list[Match]) -> Match:
    return max(options, key=lambda m: m[0].weight)


def _equipment_name(f: Facts) -> Decision | None:
    named = next((m for m in f.matched if m[0].key == "equipment" and m[1] == NAME), None)
    return (named, f.matched) if named else None


def _no_category_if(test: Callable[[Lead], bool]) -> Callable[[Facts], Decision | None]:
    return lambda f: (None, f.matched) if test(f.lead) else None


def _retail_chain(f: Facts) -> Decision | None:
    if not f.retail_brand or f.specific:
        return None
    brand = (f.retail_brand, RETAIL_BRAND)
    return brand, f.kept + [brand]


def _production_food(f: Facts) -> Decision | None:
    if not f.specific or _best(f.specific)[0].key != "food_service":
        return None
    production = (f.gtypes & config.PRODUCTION_GOOGLE_TYPES) or (f.yelp & config.YELP_PRODUCTION)
    service = (f.gtypes & config.SERVICE_GOOGLE_TYPES) or (f.yelp & config.YELP_SERVICE)
    made = [m for m in f.kept if m[0].key == "food_production"]
    return (made[0], f.kept) if production and not service and made else None


def _resort_named(name: str) -> bool:
    """The name says the place is a resort, and not one of its shops ("Solitude Mountain
    Resort Store")."""
    words = name.split()
    resort = (bool(words) and words[-1] in ("resort", "resorts")) or _words(name, config.RESORT_NAME_WORDS)
    return resort and not _words(name, config.SHOP_NAME_WORDS)


def _resort_or_mall(f: Facts) -> Decision | None:
    """A resort's or shopping mall's name decides over the shop or the homes its building
    is mapped as (config.RESORT_NAME_WORDS, config.MALL_OWN_NAME_WORDS), when no Google
    or Yelp listing says what it is and the name doesn't also say apartments."""
    if any(how != TAG for _, how in f.tagged):
        return None
    tagged = {cat.key for cat, _ in f.tagged}
    shop = any(c.startswith("shop=") for c in f.lead.raw_categories)
    if shop and tagged <= {"specialty_retail", "retail"} and _resort_named(f.name):
        key = "hospitality"
    elif (tagged == {"multifamily"} and _words(f.name, config.MALL_OWN_NAME_WORDS)
          and not _words(f.name, config.CATEGORY_BY_KEY["multifamily"].name_keywords)):
        key = "venue"
    else:
        return None
    named = (config.CATEGORY_BY_KEY[key], NAME)
    return named, f.kept if named in f.kept else f.kept + [named]


def _specific(f: Facts) -> Decision | None:
    return (_best(f.specific), f.kept) if f.specific else None


def _name_beats_type(f: Facts) -> Decision | None:
    if not f.non_prospect_type:
        return None
    blockers = f.gtypes & config.NON_PROSPECT_GOOGLE_TYPES
    allowed = (set.intersection(*(config.NAME_BEATS_GOOGLE_TYPE.get(t, set()) for t in blockers))
               if blockers else set())
    other_blocked = (bool(f.yelp & config.NON_PROSPECT_YELP_CATEGORIES) or any(
        _osm_tag_matches(f.lead, k, v) for k, v in config.NON_PROSPECT_OSM_TAGS))
    rescued = [m for m in f.by_name if m[0].key in allowed]
    return (_best(rescued), f.kept) if rescued and not other_blocked else None


def _non_prospect_type(f: Facts) -> Decision | None:
    return (None, f.kept) if f.non_prospect_type else None


def _shop(f: Facts) -> Decision | None:
    if not (any(c.startswith("shop=") for c in f.lead.raw_categories) or "store" in f.types):
        return None
    retail = (config.CATEGORY_BY_KEY["retail"], GOOGLE if "store" in f.gtypes else TAG)
    return retail, f.kept + [retail]


def _not_a_plant(f: Facts) -> Decision | None:
    telling_name = f.by_name or any(_contains_term(f.name, kw, whole=True)
                                    for cat, _ in f.tagged for kw in cat.name_keywords)
    if f.tagged and not telling_name and (_small_generic_building(f.lead) or _utility_named(f.lead)
                                          or _not_plant_named(f.name)):
        return None, f.kept
    return None


def _catch_all(f: Facts) -> Decision | None:
    return (_best(f.tagged + f.by_name), f.kept) if f.tagged or f.by_name else None


def _anything(f: Facts) -> Decision | None:
    return (_best(f.kept), f.kept) if f.kept else None


RULES = (
    Rule("equipment name", "an equipment dealer's or hauler's name decides", _equipment_name),
    Rule("not-a-prospect name", "its name says it is not a prospect (police, fire, an impound or trailer "
         "yard, parcel lockers)", _no_category_if(non_prospect_name),
         "Its name says it isn't a business that would use a compactor or baler (police, fire, an impound "
         "or trailer yard, parcel lockers)"),
    Rule("utility structure", "a pumping station, well or substation has no waste stream to compact",
         _no_category_if(utility_structure),
         "It's a pumping station, well or substation, which has no rubbish to compact"),
    Rule("self-storage", "self-storage: the tenants take their rubbish home", _no_category_if(self_storage),
         "It's self-storage: tenants take their own rubbish home, so there is little to compact on site"),
    Rule("retail chain", "a retail chain's name decides over the building it is mapped as", _retail_chain,
         "It's a known store chain, so it counts as a store even where the map shows a warehouse building"),
    Rule("production listing", "a brewery's or bakery's production listing decides over its taproom or cafe "
         "label", _production_food,
         "It's listed as a brewery or bakery that makes its products there, not only a taproom or café"),
    Rule("resort or mall name", "a resort's or shopping mall's name decides over the shop or homes its "
         "building is mapped as", _resort_or_mall,
         "Its name says it's a resort or a shopping mall, although the map shows a shop or homes there"),
    Rule("own category", "its own Google category, Yelp category or map tag decides", _specific),
    Rule("name beats type", "its name decides over a listing type that is usually not a prospect",
         _name_beats_type,
         "Its name says what it does, although its listing is a kind of place that usually isn't a prospect"),
    Rule("not-a-prospect type", "its listing type says it is not a prospect (a vet, clinic, park, gas "
         "station...)", _non_prospect_type,
         "Its listing says it's a kind of place that rarely needs a compactor or baler (a vet, clinic, park, "
         "gas station…)"),
    Rule("shop", "a shop counts as retail, whatever its name says", _shop,
         "It's listed as a shop, so it counts as a store whatever its name says"),
    Rule("not a plant", "only a catch-all industrial tag, on a small building, a utility's structure, a data "
         "centre or a career centre", _not_a_plant,
         "The map shows only an industrial building, and it's small or belongs to a utility, a data center, a "
         "job center or a city yard, so it's unlikely to need a compactor or baler"),
    Rule("catch-all tag or name", "only a catch-all industrial tag or words in its name say what it is",
         _catch_all,
         "Only the building type or the business name suggests what it does — confirm before calling"),
    Rule("search phrase", "the search phrase that found it says what it is", _anything,
         "Only the search that found it suggests what it does — confirm before calling"),
)
NO_MATCH = Rule("nothing matched", "nothing on the high-volume list matched", lambda f: (None, []))


class Classified(NamedTuple):
    best: Match | None            # the category and how it matched, or None
    matched: list[Match]          # every category that matched ("also looks like")
    rule: Rule                    # the rule that decided


def classify(lead: Lead) -> Classified:
    """The lead's category: the first of RULES that applies decides (see above)."""
    facts = Facts(lead)
    for rule in RULES:
        decided = rule.decide(facts)
        if decided is not None:
            return Classified(*decided, rule)
    return Classified(None, [], NO_MATCH)


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
    best, matched, rule = classify(lead)
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
    if rule is not NO_MATCH:
        # Which classifying rule decided (RULES), for anyone checking why a lead scored as it did.
        reasons.append(f"category rule ({rule.name}): {rule.says}")

    # A brand's helipad, pharmacy or parking lot is not the store.
    blocked = best is None and (_is_non_prospect(lead, set(lead.raw_categories))
                                or non_prospect_name(lead) or utility_structure(lead)
                                or self_storage(lead))
    brand = None if blocked else _brand_bonus(lead)
    if brand:
        score += 20
        reasons.append(f"+20 known high-volume brand ({config.brand_name(brand)})")

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
        flags.append(OWN_FLAG)
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


# ---- the explanation as a salesperson reads it
#
# lead.reasons is the scoring's own record, saved with each lead: "+35 Grocery /
# supermarket (by map tag): ...", and "category rule (own category): ..." naming the
# rule that decided, for whoever maintains the rules. The Leads page and the downloads
# show plain_reasons() of it instead, which also reads the explanations saved by earlier
# versions (saved leads are never rewritten).

# How a category matched, in a salesperson's words.
PLAIN_HOW = {GOOGLE: "from the Google listing", YELP: "from the Yelp listing", TAG: "from the map listing",
             NAME: "from its name", QUERY: "from the search that found it",
             RETAIL_BRAND: "from the store chain's name"}
_RULE_LINE = re.compile(r"category rule \(([^)]*)\)")
_HOW_PART = re.compile(r" \(by ([^)]+)\)")
_POINTS = re.compile(r"[+-]\d+ ")
# Lines of the record that read differently on the page and in the downloads.
_PLAIN_LINES = {"+0 category not on the high-volume list":
                "+0 not a kind of business that usually runs a compactor or baler"}
_PLAIN_WORDS = (("matches keyword(s): ", "matches the search words: "),)


def plain_reasons(reasons: Iterable[str]) -> list[str]:
    """A lead's explanation as the Leads page and the downloads show it: the points
    first ("+35 Grocery / supermarket (from the map listing): ..."), then the notes,
    starting with what the rule that decided its category means for a salesperson
    (Rule.note: "Only the building type or the business name suggests what it does —
    confirm before calling"), never the rule's name or the map's own terms."""
    notes_of = {rule.name: rule.note for rule in RULES}
    points: list[str] = []
    notes: list[str] = []
    for reason in reasons:
        rule = _RULE_LINE.match(reason)
        if rule:
            note = notes_of.get(rule.group(1), "")
            if note:
                notes.append(note)
            continue
        reason = _HOW_PART.sub(lambda m: f" ({PLAIN_HOW.get(m.group(1), 'from its listing')})", reason, count=1)
        reason = _PLAIN_LINES.get(reason, reason)
        for old, new in _PLAIN_WORDS:
            reason = reason.replace(old, new)
        if _POINTS.match(reason):
            points.append(reason)
        else:
            notes.append(reason[:1].upper() + reason[1:])
    return points + notes
