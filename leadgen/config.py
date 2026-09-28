"""Tunable settings: categories, keywords, brands, competitors, defaults.

Everything the client is likely to want to adjust after vetting a run lives in
this file, so tuning false positives/negatives never means touching the
pipeline code.
"""

from dataclasses import dataclass, field

# Salt Lake City, UT (downtown). Used when no location is given.
DEFAULT_LOCATION = "Salt Lake City, UT"
DEFAULT_CENTER = (40.7608, -111.8910)
DEFAULT_RADIUS_MILES = 30.0
DEFAULT_KEYWORDS = ["compactor", "baler", "waste", "recycling"]
DEFAULT_MIN_SCORE = 20

OWN_COMPANY = "Arco Compactor"

# Competitors are flagged (never dropped) so the client can see who else is
# servicing the area. Matching is done on a normalized name/website.
COMPETITORS = {
    "Pro Baler": ["probaler", "pro baler"],
    "Action Compaction": ["actioncompaction", "action compaction"],
}
SELF_ALIASES = ["arcocompactor", "arco compactor"]


@dataclass
class Category:
    key: str
    label: str
    weight: int              # base likelihood this kind of site runs a compactor/baler
    why: str                 # human-readable reason shown in the export
    google_types: list = field(default_factory=list)
    osm_tags: list = field(default_factory=list)   # (key, value or None for "any")
    name_keywords: list = field(default_factory=list)
    lead_type: str = "Prospect"


CATEGORIES = [
    Category(
        "grocery", "Grocery / supermarket", 35,
        "Grocery stores bale large volumes of cardboard and compact wet waste",
        google_types=["supermarket", "grocery_store", "warehouse_store", "wholesaler"],
        osm_tags=[("shop", "supermarket"), ("shop", "wholesale")],
        name_keywords=["supermarket", "grocery", "groceries", "food 4 less", "market place"],
    ),
    Category(
        "big_box", "Big-box / department / home improvement", 32,
        "Big-box retail runs cardboard balers and trash compactors at the dock",
        google_types=["department_store", "discount_store", "home_improvement_store",
                      "hardware_store", "furniture_store", "sporting_goods_store",
                      "electronics_store", "home_goods_store"],
        osm_tags=[("shop", "department_store"), ("shop", "doityourself"),
                  ("shop", "furniture"), ("shop", "hardware"), ("shop", "variety_store"),
                  ("shop", "electronics"), ("shop", "sports")],
    ),
    Category(
        "distribution", "Warehouse / distribution / logistics", 34,
        "Distribution and fulfillment centers generate heavy cardboard and pallet waste",
        google_types=["moving_company", "storage", "courier_service"],
        osm_tags=[("building", "warehouse"), ("industrial", "warehouse"),
                  ("industrial", "logistics"), ("office", "logistics"),
                  ("landuse", "logistics")],
        name_keywords=["distribution", "warehouse", "logistics", "fulfillment",
                       "cold storage", "freight", "supply chain", "3pl", "shipping center"],
    ),
    Category(
        "food_production", "Food & beverage production", 32,
        "Food and beverage plants produce high-volume packaging and organic waste",
        google_types=["food_manufacturer", "brewery", "winery", "bakery"],
        osm_tags=[("industrial", "food"), ("industrial", "brewery"), ("craft", "brewery"),
                  ("industrial", "bakery"), ("industrial", "dairy"),
                  ("industrial", "slaughterhouse"), ("craft", "distillery")],
        name_keywords=["foods", "food processing", "meats", "dairy", "bottling",
                       "beverage", "brewing", "creamery", "packing", "produce"],
    ),
    Category(
        "manufacturing", "Manufacturing / industrial", 28,
        "Manufacturing sites compact scrap, packaging and production waste",
        google_types=["manufacturer", "factory"],
        osm_tags=[("man_made", "works"), ("building", "industrial"),
                  ("building", "manufacture"), ("industrial", None),
                  ("landuse", "industrial")],
        name_keywords=["manufacturing", "mfg", "industries", "fabrication", "plastics",
                       "packaging", "printing", "paper", "corrugated", "container",
                       "products inc", "machining", "assembly", "plant"],
    ),
    Category(
        "healthcare", "Hospital / medical center", 28,
        "Hospitals run compactors for general waste and balers for cardboard",
        google_types=["hospital", "general_hospital"],
        osm_tags=[("amenity", "hospital"), ("healthcare", "hospital")],
        name_keywords=["hospital", "medical center", "regional medical"],
    ),
    Category(
        "venue", "Mall / stadium / arena / airport / convention", 28,
        "Large venues concentrate waste from thousands of visitors a day",
        google_types=["shopping_mall", "stadium", "arena", "airport",
                      "convention_center", "amusement_park", "event_venue"],
        osm_tags=[("shop", "mall"), ("leisure", "stadium"), ("aeroway", "aerodrome"),
                  ("amenity", "conference_centre"), ("amenity", "exhibition_centre"),
                  ("tourism", "theme_park")],
        name_keywords=["mall", "stadium", "arena", "airport", "convention center",
                       "expo center", "fashion place", "town center", "gateway"],
    ),
    Category(
        "recycling", "Recycling / waste facility", 30,
        "Recycling and transfer facilities operate balers and compactors directly",
        google_types=["recycling_center", "waste_management_service"],
        osm_tags=[("amenity", "recycling"), ("amenity", "waste_transfer_station"),
                  ("industrial", "scrap_yard"), ("landuse", "landfill"),
                  ("amenity", "waste_disposal")],
        name_keywords=["recycling", "recycle", "waste", "disposal", "sanitation",
                       "transfer station", "landfill", "scrap", "salvage"],
        lead_type="Waste / recycling facility",
    ),
    Category(
        "education", "University / college", 20,
        "Campuses run compactors at dining halls, dorms and loading docks",
        google_types=["university", "college"],
        osm_tags=[("amenity", "university"), ("amenity", "college")],
        name_keywords=["university", "college", "community college"],
    ),
    Category(
        "hospitality", "Hotel / resort", 18,
        "Larger hotels and resorts use compactors for guest and kitchen waste",
        google_types=["hotel", "resort_hotel", "lodging", "casino"],
        osm_tags=[("tourism", "hotel")],
        name_keywords=["hotel", "resort", "marriott", "hilton", "hyatt", "sheraton"],
    ),
    Category(
        "multifamily", "Apartment complex / property", 15,
        "Large apartment communities often lease trash compactors",
        google_types=["apartment_complex", "apartment_building", "housing_complex",
                      "condominium_complex", "property_management_company"],
        osm_tags=[("building", "apartments"), ("landuse", "residential")],
        name_keywords=["apartments", "apartment homes", "residences", "lofts",
                       "property management", "living"],
    ),
    Category(
        "institutional", "Government / correctional / military", 15,
        "Large institutions produce steady, high waste volumes",
        google_types=["city_hall", "courthouse", "local_government_office"],
        osm_tags=[("amenity", "prison"), ("landuse", "military"),
                  ("military", None)],
        name_keywords=["correctional", "prison", "detention", "air force base",
                       "depot", "county complex"],
    ),
    Category(
        "food_service", "Restaurant / food service", 8,
        "High-volume food service can justify a compactor (usually large sites only)",
        google_types=["restaurant", "meal_takeaway", "cafeteria", "catering_service",
                      "food_court"],
        osm_tags=[("amenity", "restaurant"), ("amenity", "food_court")],
        name_keywords=["catering", "commissary", "food service"],
    ),
    Category(
        "retail", "Other retail", 8,
        "General retail with some cardboard volume",
        google_types=["store", "clothing_store", "pet_store", "book_store",
                      "auto_parts_store", "liquor_store"],
        osm_tags=[("shop", "clothes"), ("shop", "pet"), ("shop", "car_parts"),
                  ("shop", "general"), ("shop", "books")],
    ),
    Category(
        "equipment", "Compactor / baler equipment or service", 10,
        "Name mentions compactors or balers: likely a dealer, servicer, or hauler",
        google_types=[],
        osm_tags=[],
        name_keywords=["compactor", "compaction", "baler", "baling", "dumpster",
                       "roll off", "roll-off", "hauling", "disposal service"],
        lead_type="Industry (equipment / hauler)",
    ),
]

CATEGORY_BY_KEY = {c.key: c for c in CATEGORIES}

# National/regional brands known to run balers or compactors at nearly every
# location. A brand hit adds points on top of the category weight.
HIGH_VOLUME_BRANDS = [
    "walmart", "sam's club", "sams club", "costco", "target", "smith's", "smiths",
    "harmons", "winco", "fresh market", "macey's", "maceys", "sprouts",
    "whole foods", "trader joe's", "albertsons", "lin's", "ridley's", "kroger",
    "fred meyer", "the home depot", "home depot", "lowe's", "lowes", "ikea",
    "best buy", "kohl's", "kohls", "ross dress", "tj maxx", "t.j. maxx",
    "marshalls", "hobby lobby", "dick's sporting", "sportsman's warehouse",
    "scheels", "rc willey", "r.c. willey", "deseret industries", "savers",
    "big lots", "petsmart", "petco", "michaels",
    "burlington", "at home", "amazon", "fedex", "ups", "sysco", "us foods",
    "intermountain medical", "intermountain health", "university of utah hospital",
    "st. mark's hospital", "lds hospital",
    "primary children's", "mountainview hospital", "overstock", "nordstrom",
    "macy's", "jcpenney", "dillard's", "kodiak cakes", "lehi roller mills",
    "swire coca-cola", "coca-cola", "pepsi", "frito-lay", "nestle", "kraft",
    "dannon", "northrop grumman", "l3harris", "boeing", "hill air force base",
]

# Search phrases used with the Google Places text search. The user's keywords
# are appended to this list at run time.
GOOGLE_QUERIES = [
    "supermarket", "grocery store", "wholesale club", "department store",
    "home improvement store", "warehouse", "distribution center",
    "logistics company", "fulfillment center", "cold storage", "manufacturer",
    "manufacturing plant", "food processing plant", "commercial bakery",
    "beverage bottling", "brewery", "printing company", "packaging company",
    "hospital", "shopping mall", "stadium arena", "convention center",
    "recycling center", "waste transfer station", "university", "hotel",
    "apartment complex",
]

# Overpass mirrors tried in order when the free OpenStreetMap source is used.
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]

HTTP_USER_AGENT = "compactor-lead-list-generator/1.0 (+https://github.com/thomaswright03/Compactor-Lead-List-Generator)"

# Cached API responses are reused for this long so repeat runs are free.
CACHE_TTL_SECONDS = 7 * 24 * 3600
