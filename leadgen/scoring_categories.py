"""Classifying a lead: which category of business it is (config.CATEGORIES), decided
by an ordered list of named rules (RULES) over every category that matched it, less the
matches a VETOES entry says mean nothing. See the note below Match."""

import functools
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import NamedTuple

from . import config
from .models import Lead
from .scoring_names import (
    _is_non_prospect,
    _not_campus,
    _not_plant_named,
    _only_generic_tags,
    _retail_brand,
    _shop_mapped_as_mall,
    _shop_named,
    _small_generic_building,
    _utility_named,
    clinic,
    non_prospect_name,
    parcel_station,
    self_storage,
    service_plant,
    utility_structure,
    vehicle_wash,
)
from .scoring_text import _contains_term, _osm_tag_matches, google_types, normalize, yelp_categories

# A matched category and how it matched ("Google category", "name", ...).
Match = tuple[config.Category, str]


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


def _nothing_if(test: Callable[[Lead], bool]) -> Callable[[Facts], Decision | None]:
    """No category, and nothing offered as "also looks like" (a truck wash on an
    industrial building doesn't also look like a plant)."""
    return lambda f: (None, []) if test(f.lead) else None


def _clinic(f: Facts) -> Decision | None:
    if not clinic(f.lead) or not any(cat.key == "healthcare" for cat, _ in f.matched):
        return None
    return None, [m for m in f.kept if m[0].key != "healthcare"]


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
    Rule("utility plant", "a power, heating or cooling plant is a utility building, usually another "
         "organisation's", _nothing_if(service_plant),
         "Its name says it's a power, heating or cooling plant, a utility building with little rubbish to "
         "compact"),
    Rule("vehicle wash", "a car or truck wash is a service, not a plant", _nothing_if(vehicle_wash),
         "It's a car or truck wash, which has little rubbish to compact"),
    Rule("clinic", "a clinic or outpatient centre listed as a hospital is not one", _clinic,
         "Its name says it's a clinic or outpatient center, not a hospital, so it has little rubbish to "
         "compact"),
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
