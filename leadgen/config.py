"""Tunable settings: categories, keywords, brands, competitors, defaults.

Everything the client is likely to want to adjust after vetting a run lives in
this file, so tuning false positives/negatives never means touching the
pipeline code.
"""

from dataclasses import dataclass, field

# Arco Compactor's shop: the default point the radius and "Miles" are measured
# from. Stored as coordinates so it never depends on an online geocoder. Utah's
# address records have no 876 Fortune Rd; the pin is 1876 W Fortune Rd (Fortune
# Rd is ~0.4 mi long, so any point on it gives the same distances).
OWN_ADDRESS = "876 Fortune Rd, Salt Lake City, UT 84104"
OWN_COORDS = (40.742060, -111.943408)
DEFAULT_LOCATION = OWN_ADDRESS
DEFAULT_CENTER = OWN_COORDS
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
    yelp_categories: list = field(default_factory=list)   # Yelp category aliases
    name_keywords: list = field(default_factory=list)   # whole words/phrases in the name
    lead_type: str = "Prospect"
    query_osm: bool = True   # False: tags classify results but are not fetched (too many)


CATEGORIES = [
    Category(
        "grocery", "Grocery / supermarket", 35,
        "Grocery stores bale large volumes of cardboard and compact wet waste",
        google_types=["supermarket", "grocery_store", "hypermarket", "discount_supermarket"],
        osm_tags=[("shop", "supermarket")],
        yelp_categories=["grocery", "intlgrocery", "organic_stores", "healthmarkets"],
        name_keywords=["supermarket", "grocery", "groceries", "food 4 less", "market place"],
    ),
    Category(
        "wholesale", "Warehouse club / wholesale", 34,
        "Warehouse clubs and wholesalers break down pallets of cardboard-packed goods",
        google_types=["warehouse_store", "wholesaler"],
        osm_tags=[("shop", "wholesale")],
        yelp_categories=["wholesale_stores", "wholesalers", "suppliesrestaurant"],
        name_keywords=["wholesale", "wholesalers", "warehouse club"],
    ),
    Category(
        "big_box", "Big-box / department / home improvement", 32,
        "Big-box retail runs cardboard balers and trash compactors at the dock",
        google_types=["department_store", "home_improvement_store"],
        osm_tags=[("shop", "department_store"), ("shop", "doityourself")],
        yelp_categories=["deptstores", "buildingsupplies"],
        name_keywords=["supercenter", "home improvement"],
    ),
    Category(
        "specialty_retail",
        "Mid-size retail (electronics, sporting goods, furniture, hardware, discount)", 15,
        "Mid-size stores with steady cardboard volume; chains often run balers",
        google_types=["discount_store", "hardware_store", "furniture_store",
                      "sporting_goods_store", "electronics_store", "home_goods_store"],
        osm_tags=[("shop", "furniture"), ("shop", "hardware"), ("shop", "variety_store"),
                  ("shop", "electronics"), ("shop", "sports"), ("shop", "outdoor"),
                  ("shop", "second_hand"), ("shop", "charity"), ("shop", "appliance"),
                  ("shop", "craft")],
        yelp_categories=["discountstore", "hardware", "homeandgarden", "gardening", "furniture",
                         "mattresses", "homedecor", "electronics", "computers", "appliances",
                         "sportgoods", "outdoorgear", "thrift_stores", "artsandcrafts",
                         "fabricstores", "hobbyshops", "officeequipment"],
    ),
    Category(
        "distribution", "Warehouse / distribution / logistics", 34,
        "Distribution and fulfillment centers generate heavy cardboard and pallet waste",
        osm_tags=[("building", "warehouse"), ("industrial", "warehouse"),
                  ("industrial", "logistics"), ("office", "logistics"),
                  ("landuse", "logistics")],
        name_keywords=["distribution", "distributing", "distributor", "distributors", "warehouse",
                       "warehouses", "warehousing", "logistics", "fulfillment",
                       "cold storage", "freight", "supply chain", "3pl"],
    ),
    Category(
        "food_production", "Food & beverage production", 32,
        "Food and beverage plants produce high-volume packaging and organic waste",
        osm_tags=[("industrial", "food"), ("industrial", "brewery"), ("craft", "brewery"),
                  ("industrial", "bakery"), ("industrial", "dairy"),
                  ("industrial", "slaughterhouse"), ("craft", "distillery")],
        name_keywords=["foods", "food processing", "meats", "meat packing", "dairy",
                       "bottling", "beverage", "beverages", "brewing", "creamery", "bakeries",
                       "packing plant", "packing house", "packinghouse", "packing company",
                       "fruit packing", "produce packing"],
    ),
    Category(
        "manufacturing", "Manufacturing / industrial", 28,
        "Manufacturing sites compact scrap, packaging and production waste",
        google_types=["manufacturer"],
        yelp_categories=["machineshops", "metalfabricators"],
        osm_tags=[("man_made", "works"), ("building", "industrial"),
                  ("building", "manufacture"), ("industrial", None),
                  ("landuse", "industrial")],
        name_keywords=["manufacturing", "manufacturer", "manufacturers", "mfg", "industries", "fabrication",
                       "plastics", "packaging", "printing", "corrugated", "machining",
                       "assembly plant", "manufacturing plant"],
    ),
    Category(
        "healthcare", "Hospital / medical center", 28,
        "Hospitals run compactors for general waste and balers for cardboard",
        google_types=["hospital", "general_hospital"],
        osm_tags=[("amenity", "hospital"), ("healthcare", "hospital")],
        yelp_categories=["hospitals"],
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
        yelp_categories=["shoppingcenters", "stadiumsarenas", "airports", "airportterminals",
                         "amusementparks", "waterparks", "civiccenter"],
        name_keywords=["mall", "stadium", "arena", "international airport",
                       "convention center", "expo center", "fashion place", "town center"],
    ),
    Category(
        "recycling", "Recycling / waste facility", 30,
        "Recycling and transfer facilities operate balers and compactors directly",
        osm_tags=[("amenity", "recycling"), ("amenity", "waste_transfer_station"),
                  ("industrial", "scrap_yard"), ("industrial", "auto_wrecker"),
                  ("shop", "scrap"), ("landuse", "landfill"), ("amenity", "waste_disposal")],
        yelp_categories=["recyclingcenter", "junkyards", "hazardouswastedisposal"],
        name_keywords=["recycling", "recycler", "recyclers", "recycle", "recycled", "waste",
                       "disposal",
                       "sanitation", "transfer station", "landfill", "scrap", "salvage"],
        lead_type="Waste / recycling facility",
    ),
    Category(
        "education", "University / college", 20,
        "Campuses run compactors at dining halls, dorms and loading docks",
        google_types=["university"],
        osm_tags=[("amenity", "university"), ("amenity", "college")],
        yelp_categories=["collegeuniv"],
        name_keywords=["university", "college", "community college"],
    ),
    Category(
        "hospitality", "Hotel / resort", 18,
        "Larger hotels and resorts use compactors for guest and kitchen waste",
        google_types=["hotel", "resort_hotel", "lodging", "casino"],
        osm_tags=[("tourism", "hotel")],
        yelp_categories=["hotels", "resorts", "casinos", "skiresorts"],
        name_keywords=["hotel", "resort", "marriott", "hilton", "hyatt", "sheraton"],
    ),
    Category(
        "multifamily", "Apartment complex / property", 15,
        "Large apartment communities often lease trash compactors",
        google_types=["apartment_complex", "apartment_building", "housing_complex",
                      "condominium_complex"],
        osm_tags=[("building", "apartments"), ("landuse", "residential")],
        yelp_categories=["apartments", "condominiums", "university_housing"],
        name_keywords=["apartments", "apartment homes", "residences", "lofts",
                       "property management"],
    ),
    Category(
        "institutional", "Government / correctional / military", 15,
        "Large institutions produce steady, high waste volumes",
        google_types=["city_hall", "courthouse", "local_government_office"],
        osm_tags=[("amenity", "prison"), ("landuse", "military"),
                  ("military", None)],
        yelp_categories=["jailsandprisons", "courthouses", "townhall"],
        name_keywords=["correctional", "prison", "detention", "air force base",
                       "county complex"],
    ),
    Category(
        "food_service", "Restaurant / food service", 8,
        "High-volume food service can justify a compactor (usually large sites only)",
        google_types=["restaurant", "meal_takeaway", "cafeteria", "catering_service",
                      "food_court", "fast_food_restaurant", "bakery", "brewery", "winery",
                      "brewpub", "bar", "pub", "cafe", "coffee_shop", "ice_cream_shop"],
        osm_tags=[("amenity", "restaurant"), ("amenity", "food_court"),
                  ("amenity", "fast_food"), ("amenity", "pub"), ("amenity", "bar"),
                  ("amenity", "cafe"), ("amenity", "ice_cream"), ("shop", "bakery")],
        yelp_categories=[
            "restaurants", "hotdogs", "hotdog", "food_court", "cafeteria", "catering", "buffets",
            "foodtrucks", "foodstands", "cafes", "coffee", "icecream", "desserts", "donuts",
            "bagels", "juicebars", "bubbletea", "tea", "sandwiches", "delis", "pizza", "burgers",
            "chicken_wings", "breakfast_brunch", "diners", "tradamerican", "newamerican",
            "mexican", "tacos", "chinese", "italian", "sushi", "seafood", "steak",
            "bars", "pubs", "sportsbars", "divebars", "cocktailbars", "wine_bars", "beerbar",
            "lounges", "gastropubs", "beergardens", "brewpubs",
            # production words: see YELP_PRODUCTION below
            "bakeries", "breweries", "distilleries", "wineries", "cideries", "meaderies",
            "coffeeroasteries"],
        name_keywords=["catering", "commissary", "food service"],
        query_osm=False,
    ),
    Category(
        "retail", "Other retail", 8,
        "General retail with some cardboard volume",
        # A bare "store" type is handled by the retail fallback in scoring.classify,
        # after pharmacies, gas stations etc. have been ruled out.
        google_types=["clothing_store", "pet_store", "book_store", "auto_parts_store",
                      "liquor_store"],
        osm_tags=[("shop", "clothes"), ("shop", "pet"), ("shop", "car_parts"),
                  ("shop", "general"), ("shop", "books")],
        yelp_categories=["fashion", "womenscloth", "menscloth", "childcloth", "shoes", "sportswear",
                         "outlet_stores", "petstore", "bookstores", "media", "autopartssupplies",
                         "beer_and_wine", "toys"],
        query_osm=False,
    ),
    Category(
        "equipment", "Compactor / baler equipment or service", 10,
        "Compactor/baler equipment, dumpster or hauling business: likely a dealer, servicer, "
        "or hauler",
        name_keywords=["compactor", "compactors", "compaction", "baler", "balers", "baling",
                       "dumpster", "dumpsters", "roll off", "roll-off", "roll offs",
                       "roll-offs", "hauling", "disposal service", "disposal services"],
        yelp_categories=["junkremovalandhauling", "dumpsterrental"],
        lead_type="Industry (equipment / hauler)",
    ),
]

CATEGORY_BY_KEY = {c.key: c for c in CATEGORIES}

# Catch-all tags: they make a place "industrial" or "residential" without saying much more.
GENERIC_OSM_TAGS = {("industrial", None), ("building", "industrial"), ("landuse", "industrial"),
                    ("landuse", "residential")}

# When a place's own tag/type says it is one of these, words in its name do
# not make it a prospect ("Pet Hospital" is a vet, "Dairy Queen" is fast food).
NON_PROSPECT_OSM_TAGS = (
    [("amenity", v) for v in ("veterinary", "parking", "parking_entrance", "pharmacy",
                              "clinic", "doctors", "dentist", "fuel", "library", "shelter",
                              "school", "kindergarten", "place_of_worship", "bank", "atm")]
    + [("aeroway", "helipad"), ("aeroway", "heliport"), ("tourism", "artwork"),
       ("tourism", "information"), ("leisure", None), ("healthcare", None),
       ("building", "parking"), ("building", "construction"), ("landuse", "construction")]
)
NON_PROSPECT_GOOGLE_TYPES = {
    "veterinary_care", "dentist", "dental_clinic", "doctor", "medical_clinic", "pharmacy",
    "drugstore", "church", "place_of_worship", "park", "parking", "gas_station", "library",
    "school", "primary_school", "secondary_school", "bank", "atm", "insurance_agency",
    "lawyer", "real_estate_agency", "beauty_salon", "hair_salon", "barber_shop",
    "car_repair", "car_wash", "health", "optician",
}
NON_PROSPECT_YELP_CATEGORIES = {
    "vet", "emergencypethospital", "pharmacy", "drugstores", "dentists", "generaldentistry",
    "cosmeticdentists", "pediatric_dentists", "orthodontists", "physicians", "urgent_care",
    "walkinclinics", "optometrists", "opticians", "churches", "religiousorgs", "parks",
    "dog_parks", "parking", "servicestations", "autorepair", "carwash", "oilchange",
    "libraries", "elementaryschools", "highschools", "preschools", "privateschools",
    "montessori", "childcare", "banks", "insurance", "lawyers", "realestateagents",
    "apartmentagents", "hair", "barbers", "othersalons", "beautysvc", "selfstorage", "movers",
} | {  # Yelp's doctor specialties (children of "physicians")
    "addictionmedicine", "allergist", "anesthesiologists", "audiologist", "cardiology",
    "cosmeticsurgeons", "dermatology", "earnosethroat", "emergencymedicine",
    "endocrinologists", "familydr", "fertility", "gastroenterologist", "geneticists",
    "gerontologist", "hepatologists", "hospitalists", "immunodermatologists",
    "infectiousdisease", "internalmed", "naturopathic", "nephrologists", "neurologist",
    "neuropathologists", "neurotologists", "obgyn", "oncologist", "opthamalogists",
    "orthopedists", "osteopathicphysicians", "otologists", "painmanagement", "pathologists",
    "pediatricians", "phlebologists", "plasticsurgeons", "podiatrists",
    "preventivemedicine", "proctologist", "psychiatrists", "pulmonologist", "radiologists",
    "rhematologists", "spinesurgeons", "sportsmed", "surgeons", "tattooremoval",
    "toxicologists", "underseamedicine", "urologists", "vascularmedicine",
}
# ...except that a name can still rescue these categories from these types
# ("Liberty Village Apartments" typed real_estate_agency).
NAME_BEATS_GOOGLE_TYPE = {"real_estate_agency": {"multifamily"}, "school": {"education"}}

# Google types that only say "a place exists". The search phrase that found a
# place hints at its category only when it has nothing more specific.
GENERIC_GOOGLE_TYPES = {"point_of_interest", "establishment", "premise", "food", "finance"}

# Google gives production breweries/bakeries and taprooms/cafes the same types;
# without a service type, a production word in the name decides.
PRODUCTION_GOOGLE_TYPES = {"bakery", "brewery", "winery"}
SERVICE_GOOGLE_TYPES = {"brewpub", "pub", "bar", "cafe", "coffee_shop", "meal_takeaway",
                        "fast_food_restaurant", "ice_cream_shop", "restaurant"}
# Same for Yelp: "Breweries" alone may be a plant or a taproom.
YELP_PRODUCTION = {"bakeries", "breweries", "distilleries", "wineries", "cideries", "meaderies",
                   "coffeeroasteries"}
YELP_SERVICE = (set(CATEGORY_BY_KEY["food_service"].yelp_categories) - YELP_PRODUCTION)

# The Google search phrase that found a place is a weak category hint for
# results Google tags only with GENERIC_GOOGLE_TYPES.
QUERY_CATEGORY = {
    "warehouse": "distribution", "distribution center": "distribution",
    "logistics company": "distribution", "fulfillment center": "distribution",
    "cold storage": "distribution", "manufacturer": "manufacturing",
    "manufacturing plant": "manufacturing", "printing company": "manufacturing",
    "packaging company": "manufacturing", "food processing plant": "food_production",
    "commercial bakery": "food_production", "beverage bottling": "food_production",
    "recycling center": "recycling", "waste transfer station": "recycling",
}

# Chains that almost always run a baler or compactor. A match adds points on
# top of the category weight. Distinctive names match on the name alone.
HIGH_VOLUME_BRANDS = [
    "walmart", "wal mart", "sam's club", "sams club", "costco", "winco", "whole foods",
    "trader joe's", "albertsons", "kroger", "fred meyer", "the home depot", "home depot",
    "ikea", "ross dress for less", "tj maxx", "t.j. maxx", "hobby lobby",
    "dick's sporting goods", "sportsman's warehouse", "scheels", "rc willey", "r.c. willey",
    "deseret industries", "big lots", "petsmart", "petco", "nordstrom", "jcpenney",
    "intermountain medical center", "intermountain healthcare", "intermountain health",
    "university of utah hospital", "st. mark's hospital", "lds hospital",
    "primary children's", "mountainview hospital", "kodiak cakes", "lehi roller mills",
    "swire coca-cola", "coca-cola", "coca cola", "pepsi", "pepsico", "frito-lay", "frito lay",
    "kraft heinz", "dannon", "sysco", "us foods", "northrop grumman", "l3harris", "boeing",
    "hill air force base",
]
# Brands that are also surnames or ordinary words ("Smith's Plumbing",
# "Grown Ups Daycare", "Kraft Electric"). They only count when the map's brand
# tag or the website's name says so, or the category fits (for plain retail,
# only when the name is the brand alone: "Michaels", not "Michael's Jewelers").
AMBIGUOUS_BRANDS = {
    **{b: {"grocery"} for b in ("smith's", "smiths", "lin's", "ridley's", "macey's",
                                "maceys", "harmons", "fresh market", "sprouts")},
    **{b: {"big_box", "specialty_retail", "retail", "grocery"}
       for b in ("target", "lowe's", "lowes", "kohl's", "kohls", "macy's", "dillard's",
                 "burlington", "michaels", "savers", "at home", "marshalls", "best buy",
                 "overstock")},
    **{b: {"food_production", "manufacturing"} for b in ("kraft", "nestle")},
    **{b: {"distribution"} for b in ("ups", "fedex", "amazon")},
}
BRAND_EXCLUDE = ["the ups store", "fedex office", "ups access point", "amazon hub",
                 "amazon locker"]

# Review counts as a sign of a busy site: (at least this many reviews, points).
# Yelp counts run far lower than Google's for the same store.
REVIEW_BONUS = {
    "Google": [(2000, 15), (500, 10), (100, 5)],
    "Yelp": [(400, 15), (150, 10), (40, 5)],
}

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

# Yelp searches by category (more precise than words for Yelp), most useful
# first; the user's keywords and the competitor names are searched as words.
# Label (shown in "Found By") -> Yelp category aliases, matched as "any of".
YELP_SEARCHES = {
    "grocery stores": ["grocery", "intlgrocery", "organic_stores"],
    "warehouse clubs and wholesalers": ["wholesale_stores", "wholesalers", "suppliesrestaurant"],
    "department and discount stores": ["deptstores", "discountstore"],
    "home improvement and building supplies": ["buildingsupplies", "hardware"],
    "hospitals": ["hospitals"],
    "shopping centers": ["shoppingcenters"],
    # Not "venues": Yelp gives that to restaurants and hotels with an event room.
    "stadiums, arenas and airports": ["stadiumsarenas", "civiccenter", "airports",
                                      "amusementparks", "waterparks"],
    "recycling and scrap": ["recyclingcenter", "junkyards", "hazardouswastedisposal"],
    "hotels and resorts": ["hotels", "resorts", "casinos", "skiresorts"],
    "colleges, universities and jails": ["collegeuniv", "jailsandprisons"],
    "apartments": ["apartments", "condominiums", "university_housing"],
    "furniture, electronics and sporting goods": ["furniture", "electronics", "appliances",
                                                  "sportgoods", "thrift_stores"],
    "machine shops and fabricators": ["machineshops", "metalfabricators"],
}
# Yelp's trial allows about 300 calls a day, so a run stops here unless a
# request cap is given. Yelp's terms allow caching its data for 24 hours only.
YELP_DEFAULT_MAX_REQUESTS = 200
YELP_CACHE_TTL_SECONDS = 24 * 3600

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
