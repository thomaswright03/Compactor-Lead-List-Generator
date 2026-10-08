"""The words and map tags the classifying rules read (scoring_categories.py,
scoring_names.py): names and tags that say a place is not the campus, not a plant, not a
prospect, a utility's structure, self-storage, a parcel station, a clinic, a car wash,
only a building or only a generic word. Read as config.X (config.py takes in this
module)."""

from .config_categories import CATEGORY_BY_KEY

# A place named after a university or college is not the campus when its name says it
# is a garden, house, condo, department, press, library, office... ("University Heights
# Condominiums", "Department of Linguistics, University of Utah", "University of Utah
# Press"): the campus itself is the lead, not each of its units and neighbours. Whole
# words; "college of" / "school of" count only after the start ("Price College of
# Engineering" is a unit, "College of Eastern Utah" is a college).
NOT_CAMPUS_WORDS = [
    "garden", "gardens", "house", "home", "homes", "residence", "residences", "heights",
    "condominium", "condominiums", "condo", "condos", "apartments", "townhomes", "village",
    "department", "dept", "press", "institute", "center for", "centre for", "office",
    "offices", "bookstore", "library", "museum", "parking", "credit union", "alumni",
    "foundation", "club", "chapel", "church", "ward", "stake", "lab", "laboratory",
    "laboratories", "clinic", "preschool", "child care", "daycare", "observatory", "park",
    "station", "president's house", "presidents house",
]
NOT_CAMPUS_INNER = ["college of", "school of"]

# Catch-all tags: they make a place "industrial" or "residential" without saying much more.
GENERIC_OSM_TAGS = {("industrial", None), ("building", "industrial"), ("landuse", "industrial"),
                    ("landuse", "residential")}

# Kinds of site that spread over a mile or more (an air base, an airport, a campus): two
# listings with the same name and one of these tags are one site within
# dedupe.LARGE_SITE_MILES ("Hill Air Force Base" as the airfield and as the base).
LARGE_SITE_OSM_TAGS = [("landuse", "military"), ("military", None), ("aeroway", "aerodrome"),
                       ("amenity", "university"), ("amenity", "college")]
LARGE_SITE_TYPES = {"airport", "university", "yelp:airports", "yelp:collegeuniv"}

# A production word in a name ("dairy", "foods", "meats") alone doesn't make a plant when
# the name also says it is a small shop or eatery ("Day Dairy Barn", "Sunrise Meats
# Market", "Dairy Queen"): such a name gives no food & beverage production category.
NOT_PRODUCTION_NAME_WORDS = ["barn", "shop", "shoppe", "store", "market", "mart", "cafe",
                             "deli", "bar", "grill", "kitchen", "diner", "stand", "queen",
                             "freeze", "drive in", "treats", "ice cream", "corner",
                             "express", "restaurant", "eatery", "bistro"]

# When a place's own tag/type says it is one of these, words in its name do
# not make it a prospect ("Pet Hospital" is a vet, "Dairy Queen" is fast food).
NON_PROSPECT_OSM_TAGS = (
    [("amenity", v) for v in ("veterinary", "parking", "parking_entrance", "pharmacy",
                              "clinic", "doctors", "dentist", "fuel", "library", "shelter",
                              "school", "kindergarten", "place_of_worship", "bank", "atm",
                              "police", "fire_station", "parcel_locker", "vehicle_impound",
                              "parking_space")]
    + [("aeroway", "helipad"), ("aeroway", "heliport"), ("tourism", "artwork"),
       ("tourism", "information"), ("leisure", None), ("healthcare", None),
       ("building", "parking"), ("building", "construction"), ("landuse", "construction")]
)
# Names that say a place is not a prospect whatever its tags or other words say:
# police, fire and impound sites, trailer and truck yards, public fleet yards and
# parcel lockers ("Herriman Police Impound Lot", "Salt Lake City Fire Department
# Training and Logistics Center", "Amazon DUT2 Trailer Yard", "Luxer One"). Only a
# specific recycling or transfer-station tag (NAME_BLOCK_RESCUE_TAGS) still counts.
NON_PROSPECT_NAME_WORDS = [
    "police", "sheriff", "sheriffs", "fire department", "fire station", "fire training",
    "fire rescue", "impound", "tow yard", "towing yard", "trailer yard", "trailer parking",
    "trailer storage", "truck parking", "truck yard", "parking lot", "park and ride",
    "fleet maintenance", "fleet management", "fleet services", "maintenance yard",
    "luxer", "luxer one", "parcel locker", "parcel lockers", "package locker",
    "package lockers", "amazon locker", "amazon hub",
]
# Utility and infrastructure structures: a pumping station, water well, substation or
# water tank is not a business with a waste stream ("6th East Well", tagged
# building=industrial and man_made=pumping_station). No prospect category, whatever
# catch-all tag or other word says so; only a NAME_BLOCK_RESCUE_TAGS tag still counts.
UTILITY_OSM_TAGS = [
    ("man_made", "pumping_station"), ("man_made", "water_well"), ("man_made", "water_tower"),
    ("man_made", "reservoir_covered"), ("man_made", "storage_tank"), ("man_made", "street_cabinet"),
    ("man_made", "monitoring_station"), ("man_made", "telephone_office"),
    ("power", "substation"), ("power", "transformer"), ("power", "switch"),
    ("building", "transformer_tower"), ("building", "pumping_station"),
    ("pumping_station", None), ("substation", None), ("telecom", None),
    ("building", "data_center"), ("building", "data_centre"),
]
# A place known only by a catch-all industrial tag (building=industrial,
# landuse=industrial) is a utility structure when its name or its operator tag says
# so ("Salt Lake City Corp", "Utah Power & Light Co", "Pacificorp", "UTA Station";
# operator "American Fork City"). A telling name ("... Recycling") or a specific tag
# still decides first.
UTILITY_NAME_WORDS = [
    "well", "pump station", "pumping station", "pump house", "lift station",
    "booster station", "substation", "sub station", "water tank", "reservoir",
    "power and light", "power light", "pacificorp", "rocky mountain power", "dominion energy",
    "questar", "enbridge", "uta", "trax", "frontrunner", "city corp", "city corporation",
    "irrigation", "canal company", "water district", "water conservancy",
    "improvement district", "sewer district", "telephone", "centurylink", "qwest",
]
UTILITY_OPERATOR_WORDS = [
    "city", "town", "county", "district", "state of utah", "utah department",
    "department of transportation", "udot", "uta", "utah transit authority", "power",
    "light", "energy", "gas", "water", "utility", "utilities", "pacificorp", "questar",
    "dominion", "enbridge", "irrigation", "canal", "sewer", "telephone", "centurylink",
    "qwest", "lumen",
]
# A building with only a catch-all industrial tag and a footprint under this many
# square feet is a shed, pump house or small utility building, not a plant: no
# prospect category (a specific tag, a telling name or a footprint this big or more
# still counts).
SMALL_GENERIC_BUILDING_SQFT = 5_000

# A manufacturing word in a name ("industries") doesn't make a plant when the name or
# the map says it is a shop ("Deseret Industries Thrift Store", tagged landuse=retail).
NOT_MANUFACTURING_NAME_WORDS = ["store", "stores", "thrift", "outlet", "boutique",
                                "showroom", "retail"]
RETAIL_OSM_TAGS = [("landuse", "retail")]

# Self-storage (units rented to the public, "Magna Safe Storage", "Storage Warehouse",
# tagged shop=storage_rental) is not a warehouse prospect: tenants take their rubbish
# home. A name counts when it has one of SELF_STORAGE_NAME_WORDS, or the word "storage"
# without a word that makes it a logistics or cold store (COLD_STORAGE_WORDS,
# LOGISTICS_NAME_WORDS: "Lineage Cold Storage", "Acme Storage & Distribution"). A
# specific recycling tag (NAME_BLOCK_RESCUE_TAGS) still counts.
SELF_STORAGE_OSM_TAGS = [("shop", "storage_rental"), ("amenity", "storage_rental"),
                         ("building", "storage_rental")]
SELF_STORAGE_GOOGLE_TYPES = {"storage", "self_storage"}
SELF_STORAGE_NAME_WORDS = ["self storage", "selfstorage", "self-storage", "safe storage",
                           "storage units", "storage unit", "mini storage", "ministorage",
                           "rv storage", "boat storage", "storage rental", "storage rentals",
                           "public storage", "extra space storage", "cubesmart",
                           "u haul storage"]
COLD_STORAGE_WORDS = ["cold", "frozen", "freezer", "refrigerated", "food", "foods", "grain",
                      "bulk", "records", "document", "archive", "archives"]
LOGISTICS_NAME_WORDS = ["distribution", "distributing", "logistics", "fulfillment",
                        "warehousing", "supply chain", "3pl", "freight", "transport",
                        "transportation", "trucking", "shipping"]

# Places a catch-all industrial tag (building=industrial) doesn't make a plant: a data
# centre, a job or career centre, a city's or county's maintenance shops and yards
# ("Flexential Salt Lake City - Downtown", "Utah Career Center", "Woods Cross City
# Shops"). No prospect category; a specific tag or a telling name still decides first.
NOT_PLANT_NAME_WORDS = [
    "data center", "data centre", "datacenter", "data centers", "colocation", "flexential",
    "databank", "equinix", "digital realty", "cyrusone", "aligned data",
    "career center", "career centre", "job center", "employment center", "workforce services",
    "american job center", "city shops", "county shops", "public works", "city yard",
    "city yards", "county yard", "streets division", "street department", "road shed",
    "roads shed", "maintenance shops",
]

# A parcel carrier's delivery station or home-delivery hub is a warehouse / logistics
# site (cardboard by the trailer-load), whatever building it is mapped as: its name or
# its operator / brand tag names a carrier (PARCEL_CARRIERS), and one of them says it is a
# station or hub (PARCEL_STATION_WORDS): "FedEx Home Delivery" (operator FedEx),
# "Amazon Delivery Station DSL5", "UPS Customer Center". A carrier's shop ("The UPS
# Store", "FedEx Office") or post office has no such word; its lockers are
# NON_PROSPECT_NAME_WORDS.
PARCEL_CARRIERS = ["amazon", "amazon logistics", "fedex", "fedex ground", "ups", "united parcel service",
                   "usps", "united states postal service", "us postal service", "dhl", "ontrac",
                   "lasership", "purolator"]
PARCEL_STATION_WORDS = ["home delivery", "delivery station", "delivery center", "delivery centre",
                        "delivery hub", "delivery depot", "sort center", "sortation center",
                        "sorting center", "sorting facility", "processing center",
                        "processing and distribution", "ground hub", "customer center", "hub",
                        "depot"]

# Retail chains whose name holds a warehouse or industrial word ("Harbor Freight" sells
# tools; the word "freight" says nothing): the name counts for the retail category
# given here, never as a warehouse or plant.
RETAIL_NAME_BRANDS = {
    "harbor freight": "specialty_retail", "harbor freight tools": "specialty_retail",
    "sportsman's warehouse": "specialty_retail", "sportsmans warehouse": "specialty_retail",
    "the container store": "specialty_retail", "mattress warehouse": "specialty_retail",
    "furniture warehouse": "specialty_retail", "shoe warehouse": "retail",
    "dsw designer shoe warehouse": "retail", "men's wearhouse": "retail",
}

# A shop mapped as a mall (a furniture store tagged shop=mall) is the shop, not a
# venue, unless its name says it is a shopping centre (MALL_NAME_WORDS).
MALL_NAME_WORDS = ["mall", "shopping center", "shopping centre", "shopping plaza", "plaza",
                   "town center", "towne center", "town centre", "marketplace", "commons",
                   "crossing", "outlets", "outlet mall", "galleria", "square", "promenade",
                   "fashion place", "gateway", "station park", "city creek", "center", "centre",
                   "village", "shops at", "the shops"]
# A resort's or a shopping mall's own name says what it is, over the shop or the homes its
# building is mapped as (scoring rule "resort or mall name"): "Snowbird Ski & Summer
# Resort", an office tagged shop=sports, is the resort; "Galleria Mall", a building tagged
# building=apartments, is the mall. A name whose last word is "resort" counts too
# ("Deer Valley Resort"), not one that only starts with it ("Resort Sports"), nor a
# resort's shop (SHOP_NAME_WORDS: "Solitude Mountain Resort Store").
RESORT_NAME_WORDS = ["ski resort", "ski area", "summer resort", "mountain resort", "ski and summer resort"]
MALL_OWN_NAME_WORDS = ["mall", "shopping mall", "shopping center", "shopping centre", "outlet mall"]
SHOP_NAME_WORDS = ["furniture", "mattress", "mattresses", "appliance", "appliances", "store",
                   "shop", "boutique", "showroom", "hardware", "tools", "carpet", "flooring",
                   "rugs", "jewelers", "jewelry", "books", "home furnishings", "interiors",
                   "decor", "gallery", "music", "pianos", "outdoor", "sports", "clothing"]

# A hospital's map listing named as a clinic or an outpatient centre is a clinic, not a
# hospital ("Intermountain West Valley Clinic", tagged amenity=hospital): like the
# clinics NON_PROSPECT_* leave out, it has little rubbish to compact. A name that also
# says "hospital" stays a hospital.
CLINIC_NAME_WORDS = ["clinic", "clinics", "instacare", "urgent care", "outpatient", "health center",
                     "health centre", "surgery center", "surgical center", "dialysis", "imaging center",
                     "family medicine", "family practice"]
# A power, heating or cooling plant is a utility building, usually another organisation's
# ("Hospital Generation Plant", a hospital's power plant): no prospect category, whatever
# its industrial tag or the other words of its name say.
SERVICE_PLANT_NAME_WORDS = ["generation plant", "generating plant", "generating station", "power plant",
                            "power station", "central plant", "central utility plant", "utility plant",
                            "heating plant", "cooling plant", "chiller plant", "chilled water plant",
                            "steam plant", "boiler plant", "boiler house", "cogeneration", "energy plant",
                            "thermal plant"]
# A car or truck wash ("Salt Lake Diesel Wash" on an industrial building) is a service
# business with little rubbish to compact, not a plant; by its name or its map tag.
VEHICLE_WASH_NAME_WORDS = ["car wash", "carwash", "car washes", "truck wash", "truckwash", "diesel wash",
                           "auto wash", "vehicle wash", "bus wash", "rv wash", "semi wash", "fleet wash",
                           "wash bay", "blue beacon"]
VEHICLE_WASH_OSM_TAGS = [("amenity", "car_wash")]

# A map name that only describes a building ("Office and Warehouse, West", "Warehouse
# Building B"): each word a kind of building (BUILDING_KIND_WORDS, at least one), a word
# naming a part or side of a site, a joining word, a number or a single letter. With no
# address, phone or website it is no business anyone can call: it is held at
# NEEDS_NAME_MAX_SCORE (config.py; tier D, below the default minimum score, so a search
# leaves it out) and flagged NEEDS_NAME_FLAG (scoring.mark_needs_name), saved rows too as
# they are shown (the saved row is unchanged).
BUILDING_KIND_WORDS = ["office", "offices", "warehouse", "warehouses", "building", "buildings", "bldg",
                       "industrial", "manufacturing", "plant", "factory", "shop", "shops", "storage",
                       "hangar", "hangars", "garage", "garages", "annex", "facility", "facilities",
                       "complex", "center", "centre", "dock", "docks", "loading", "maintenance", "flex",
                       "space", "suite", "suites", "unit", "units", "tower", "wing", "hall", "yard",
                       "distribution", "retail", "commercial", "business", "park"]
BUILDING_FILLER_WORDS = ["north", "south", "east", "west", "northeast", "northwest", "southeast", "southwest",
                         "upper", "lower", "main", "front", "rear", "back", "central", "new", "old", "mixed",
                         "use", "light", "and", "the", "of", "with", "a", "no"]
NEEDS_NAME_FLAG = "Needs a name: only a building on the map, with no business name, address or phone"


# A place whose map name is only a generic word ("Recycling", "Junkyard") gives a
# salesperson nothing to look up: the map data's name is made descriptive (its
# operator, else "at" its street, else its city; osm.parse_element), and one still
# known only by the word scores GENERIC_NAME_PENALTY less, so named places rank first.
GENERIC_NAMES = [
    "recycling", "recycling center", "recycling centre", "recycling drop off",
    "recycling dropoff", "recycling drop-off", "recycling bins", "recycling containers",
    "recycle", "recycle center", "junkyard", "junk yard", "scrap yard", "scrapyard",
    "salvage yard", "landfill", "transfer station", "dump", "waste", "trash", "garbage",
    "compactor", "dumpster", "dumpsters", "warehouse", "storage", "self storage",
    "distribution center", "factory", "plant", "industrial", "industrial building",
    "manufacturing", "shop", "store", "hotel", "apartments", "hospital", "grocery",
    "supermarket", "mall",
]
GENERIC_NAME_PENALTY = 15

NAME_BLOCK_RESCUE_TAGS = {("amenity", "recycling"), ("amenity", "waste_transfer_station"),
                          ("landuse", "landfill")}
NAME_BLOCK_RESCUE_GOOGLE_TYPES = {"waste_transfer_station", "recycling_center"}
NON_PROSPECT_GOOGLE_TYPES = {
    "veterinary_care", "dentist", "dental_clinic", "doctor", "medical_clinic", "pharmacy",
    "drugstore", "church", "place_of_worship", "park", "parking", "gas_station", "library",
    "school", "primary_school", "secondary_school", "bank", "atm", "insurance_agency",
    "lawyer", "real_estate_agency", "beauty_salon", "hair_salon", "barber_shop",
    "car_repair", "car_wash", "health", "optician", "police", "fire_station",
}
NON_PROSPECT_YELP_CATEGORIES = {
    "vet", "emergencypethospital", "pharmacy", "drugstores", "dentists", "generaldentistry",
    "cosmeticdentists", "pediatric_dentists", "orthodontists", "physicians", "urgent_care",
    "walkinclinics", "optometrists", "opticians", "churches", "religiousorgs", "parks",
    "dog_parks", "parking", "servicestations", "autorepair", "carwash", "oilchange",
    "libraries", "elementaryschools", "highschools", "preschools", "privateschools",
    "montessori", "childcare", "banks", "insurance", "lawyers", "realestateagents",
    "apartmentagents", "hair", "barbers", "othersalons", "beautysvc", "selfstorage", "movers",
    "policedepartments", "firedepartments", "towing",
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
