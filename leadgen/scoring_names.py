"""What a listing's name and tags say it is, or that it is no prospect: a campus's
neighbour, a utility's structure, self-storage, a clinic, a car wash, a name that only
describes a building... The classifying rules (scoring_categories.py) and the scoring
(scoring.py) read these; the words and tags themselves are in config."""

import re
from collections.abc import Iterable

from . import config
from .models import Lead
from .scoring_text import _contains_term, _osm_tag_matches, _term_pattern, google_types, normalize, yelp_categories


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


def _named(lead: Lead, words: Iterable[str]) -> bool:
    name = normalize(lead.name)
    return any(_contains_term(name, w, whole=True) for w in words)


def clinic(lead: Lead) -> bool:
    """True when the name says the place is a clinic or an outpatient centre, not a
    hospital (config.CLINIC_NAME_WORDS, and no "hospital" in the name)."""
    return _named(lead, config.CLINIC_NAME_WORDS) and not _named(lead, ["hospital", "hospitals"])


def service_plant(lead: Lead) -> bool:
    """True when the name says the place is a power, heating or cooling plant, a utility
    building, usually another organisation's ("Hospital Generation Plant";
    config.SERVICE_PLANT_NAME_WORDS)."""
    return _named(lead, config.SERVICE_PLANT_NAME_WORDS)


def vehicle_wash(lead: Lead) -> bool:
    """True for a car or truck wash, by its name or its map tag (config.VEHICLE_WASH_*)."""
    return (_named(lead, config.VEHICLE_WASH_NAME_WORDS)
            or any(_osm_tag_matches(lead, k, v) for k, v in config.VEHICLE_WASH_OSM_TAGS))


def _building_name(name: str) -> bool:
    """True when a name only describes a building ("Office and Warehouse, West":
    config.BUILDING_KIND_WORDS and BUILDING_FILLER_WORDS, numbers, single letters)."""
    words = normalize(name).split()
    kinds, fillers = set(config.BUILDING_KIND_WORDS), set(config.BUILDING_FILLER_WORDS)
    return (any(w in kinds for w in words)
            and all(w in kinds or w in fillers or w.isdigit() or len(w) == 1 for w in words))


# A generic name made descriptive by its street ("Recycling at 1200 W 500 S", osm_parse._descriptive).
_AT_STREET = re.compile(r"\s(?:at|near)\s", re.I)


def needs_name(lead: Lead) -> bool:
    """True when the listing's name only describes a building (_building_name) or is only
    a generic word, alone or with its town ("Recycling", "Recycling, Magna": generic_name,
    not "Recycling at 1200 W 500 S"), and it gives no address, phone or website: there is
    no business to look up or call until someone finds out who is there."""
    if lead.address or lead.phone or lead.website:
        return False
    return _building_name(lead.name) or (generic_name(lead.name) and not _AT_STREET.search(lead.name))


def generic_name(name: str) -> bool:
    """True when a name is only a generic word ("Recycling", "Junkyard"), alone or made
    descriptive only by its street or city ("Recycling at 1200 W 500 S")."""
    base = re.split(r"\s+(?:at|near)\s+|,", name.strip(), maxsplit=1)[0]
    return normalize(base) in {normalize(g) for g in config.GENERIC_NAMES}
