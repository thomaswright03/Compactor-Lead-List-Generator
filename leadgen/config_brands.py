"""High-volume brands (their bonus, how each is written, the ones that are also place
names or ordinary words) and the busy-site bonus by review count. Read as config.X
(config.py takes in this module)."""

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
# How a brand is written in the score's reasons and downloads, where plain capitals
# ("Sam's Club", "Hill Air Force Base") would be wrong; every other brand gets them.
BRAND_NAMES = {
    "walmart": "Walmart", "wal mart": "Walmart", "sams club": "Sam's Club", "winco": "WinCo",
    "trader joe's": "Trader Joe's", "ikea": "IKEA", "tj maxx": "TJ Maxx", "t.j. maxx": "T.J. Maxx",
    "rc willey": "RC Willey", "r.c. willey": "R.C. Willey", "petsmart": "PetSmart",
    "jcpenney": "JCPenney", "lds hospital": "LDS Hospital", "mountainview hospital": "MountainView Hospital",
    "st. mark's hospital": "St. Mark's Hospital", "coca cola": "Coca-Cola", "frito lay": "Frito-Lay",
    "pepsico": "PepsiCo", "us foods": "US Foods", "l3harris": "L3Harris", "smiths": "Smith's",
    "maceys": "Macey's", "lowes": "Lowe's", "kohls": "Kohl's", "ups": "UPS", "fedex": "FedEx",
    "university of utah hospital": "University of Utah Hospital", "ross dress for less": "Ross Dress for Less",
}


def brand_name(brand: str) -> str:
    """A matched brand as it is written: "harmons" -> "Harmons", "sam's club" -> "Sam's Club"."""
    if brand in BRAND_NAMES:
        return BRAND_NAMES[brand]
    return " ".join("-".join(part[:1].upper() + part[1:] for part in word.split("-"))
                    for word in brand.split())


# High-volume "brands" that are also place names: a name counts for them only when it
# starts with the brand ("Tru by Hilton Clearfield Hill Air Force Base" is a hotel near
# the base, not the base). The map's own brand or operator tag always counts.
PLACE_NAME_BRANDS = ["hill air force base", "intermountain medical center",
                     "university of utah hospital"]
# Words before a brand in a name that make it a location, not the business
# ("Hotel near Costco", "Parking at Walmart").
BRAND_LOCATION_WORDS = ["near", "at", "by", "across from", "behind", "next to", "inside",
                        "off", "opposite"]
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
