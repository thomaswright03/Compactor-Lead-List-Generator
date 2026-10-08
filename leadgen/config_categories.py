"""The categories of business the scoring knows, each with its weight (how likely such a
site runs a compactor or baler) and the Google types, map tags, Yelp categories and name
words that say a listing is one. Read as config.CATEGORIES and config.CATEGORY_BY_KEY
(config.py takes in this module)."""

from dataclasses import dataclass, field


@dataclass
class Category:
    key: str
    label: str
    weight: int              # base likelihood this kind of site runs a compactor/baler
    why: str                 # human-readable reason shown in the export
    google_types: list[str] = field(default_factory=list)
    osm_tags: list[tuple[str, str | None]] = field(default_factory=list)   # (key, value or None for "any")
    yelp_categories: list[str] = field(default_factory=list)   # Yelp category aliases
    name_keywords: list[str] = field(default_factory=list)   # whole words/phrases in the name
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
        name_keywords=["thrift store", "thrift stores", "thrift shop", "deseret industries"],
    ),
    Category(
        "distribution", "Warehouse / distribution / logistics", 34,
        "Distribution and fulfillment centers generate heavy cardboard and pallet waste",
        # amenity=post_depot: a parcel carrier's delivery station or sorting depot.
        osm_tags=[("building", "warehouse"), ("industrial", "warehouse"),
                  ("industrial", "logistics"), ("office", "logistics"),
                  ("landuse", "logistics"), ("amenity", "post_depot")],
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
