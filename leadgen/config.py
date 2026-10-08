"""Tunable settings: categories, keywords, brands, competitors, defaults.

Everything the client is likely to want to adjust after vetting a run lives in
this file and the three it takes in, so tuning false positives/negatives never means
touching the pipeline code: config_categories.py (the categories of business and their
weights), config_names.py (the words and map tags the classifying rules read) and
config_brands.py (high-volume brands, the busy-site bonus). Every setting is read as
config.X, wherever it is written.
"""

from .config_brands import *  # noqa: F403 - read as config.X (see above)
from .config_categories import *  # noqa: F403
from .config_names import *  # noqa: F403

# AARCO's shop: the default point the radius and "Miles" are measured
# from. Stored as coordinates so it never depends on an online geocoder. Utah's
# address records have no 876 Fortune Rd; the pin is 1876 W Fortune Rd (Fortune
# Rd is ~0.4 mi long, so any point on it gives the same distances).
OWN_ADDRESS = "876 Fortune Rd, Salt Lake City, UT 84104"
OWN_COORDS = (40.742060, -111.943408)
DEFAULT_LOCATION = OWN_ADDRESS
DEFAULT_CENTER = OWN_COORDS
DEFAULT_RADIUS_MILES = 30.0
# AARCO's area: searches are meant for about this far around the shop (SERVICE_CENTER).
# A search whose place is farther away is only started once the person confirms it,
# naming the place and its distance, so a typo or a same-named town in another state
# never uses up the day's search (web/finding.py). The command line warns instead.
SERVICE_CENTER = OWN_COORDS
SERVICE_AREA_MILES = 30.0
DEFAULT_KEYWORDS = ["compactor", "baler", "waste", "recycling"]
DEFAULT_MIN_SCORE = 20
# A listing with no business name, address or phone (config_names.BUILDING_KIND_WORDS,
# GENERIC_NAMES) is held just below it, in tier D (scoring.mark_needs_name).
NEEDS_NAME_MAX_SCORE = DEFAULT_MIN_SCORE - 1
# The version of the scoring rules (the categories, weights, brands and name words in this
# file and the three it takes in, and the rules in scoring.py and the scoring_*.py modules
# beside it). Raise it by hand whenever a change moves scores or tiers, and say so in the
# commit; /healthz and `python -m leadgen scoring-report` show it, so a saved score can be
# matched to the rules that made it (docs/scoring-evaluation.md).
SCORING_VERSION = 2

# The client's name, as every page title, the brand block, the login and error pages,
# the own-listing flag and the problem alerts write it. OWN_COMPANY_SHORT is the brand
# block's name on a phone, where the full name doesn't fit beside the Menu button.
# Earlier names (AARCO Compactors & Balers, AARCO Compactor) still read as this one in
# saved flags and search records (scoring.current_own_flag, daily._OLD_NAME).
OWN_COMPANY = "AARCO Waste & Disposal"
OWN_COMPANY_SHORT = "AARCO"
SITE_NAME = f"{OWN_COMPANY} Lead Finder"
# Whom the login page names for access or a forgotten password (the owner's choice,
# 2026-10-07); the phone number becomes a tap-to-call link. LEADGEN_SUPPORT_CONTACT in
# the environment replaces it.
SUPPORT_CONTACT = "Thomas Wright at (801) 580-8630"

# Competitors are flagged (never dropped) so the client can see who else is
# servicing the area. A name matches as whole words ("Pro-Baler Inc", "Action Compaction
# Services") or written as one word opening the name or a part of the website
# ("probaler.com"), never inside another word ("Repro Balers", "Transaction
# Compaction"); SELF_ALIASES and PARENT_ALIASES match the same way (scoring_flags._holds_alias).
COMPETITORS = {
    "Pro Baler": ["probaler", "pro baler"],
    "Action Compaction": ["actioncompaction", "action compaction"],
}
# AARCO's own listing, under its name (AARCO Waste & Disposal), an earlier one (AARCO
# Compactors & Balers, AARCO Compactor) or the spelling "Arco" a listing may use.
SELF_ALIASES = ["aarcowaste", "aarco waste", "aarcocompactor", "aarco compactor", "arcocompactor",
                "arco compactor"]
# AARCO is a division of ACE Recycling & Disposal: the parent company's listings are
# treated exactly like AARCO's own (flagged, never a prospect, out of "Not checked" and
# Stats). A listing is the parent's when its name or another name it goes by holds one of
# PARENT_ALIASES as whole words ("Ace Intermountain Recycling Center"; "Ace Hardware" is
# not), its website is on one of PARENT_DOMAINS, or its street address is one of
# PARENT_ADDRESSES (street, then town; a listing in another town is not). The owner
# confirms the list: add a name, domain or facility here, no code change needed.
PARENT_COMPANY = "ACE Recycling & Disposal"
PARENT_ALIASES = ["ACE Recycling & Disposal", "ACE Recycling and Disposal", "ACE Disposal",
                  "Ace Intermountain Recycling", "Ace Recycling"]
PARENT_DOMAINS = ["acedisposal.com"]
PARENT_ADDRESSES = ["1240 South Wallace Road, Salt Lake City"]


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
# The website may make at most this many Yelp calls in any 24 hours in total,
# across all searches (the same Yelp key is used elsewhere): a rolling window,
# so each call frees up again 24 hours after it was made (usage.py). Nothing in
# the form can raise it.
YELP_DAILY_LIMIT = 50
# A run stops here unless a (smaller) request cap is given.
YELP_DEFAULT_MAX_REQUESTS = YELP_DAILY_LIMIT
# A Yelp search is reused (and continued deeper) for 7 days, so repeat searches
# of an area don't spend the 50 daily calls again.
YELP_CACHE_TTL_SECONDS = 7 * 24 * 3600
# The map-data step's first round (every part, every mirror) gives up after this long,
# so a search never hangs when the free map servers are down (see OVERPASS_RETRY_*).
OVERPASS_DEADLINE_SECONDS = 240
# The public map servers often refuse or time out on one query for a whole 30-mile
# circle, so a wide search is asked in parts: a grid of boxes up to this many miles
# wide (a 30-mile search is 9 parts), this many at a time. A part gets at most
# OVERPASS_PART_SECONDS; one that fails is asked again as four smaller quarters
# (OVERPASS_SPLITS times at most), and a part no server answered leaves the search
# incomplete (what the other parts found is kept).
OVERPASS_PART_MILES = 20
OVERPASS_PARALLEL = 2
OVERPASS_PART_SECONDS = 90
OVERPASS_SPLITS = 1
# Parts still missing after OVERPASS_DEADLINE_SECONDS (busy or throttling servers) are
# asked again automatically, in up to this many more rounds, each after a pause (so a
# throttling server cools down) and with its own time. The whole map-data step is so
# bounded by 240 + 1 x (20 + 150) seconds, about 7 minutes; most searches need none.
# Parts still missing after that are filled in in the background (FILL_IN_*).
OVERPASS_RETRY_ROUNDS = 1
OVERPASS_RETRY_PAUSE_SECONDS = 20
OVERPASS_RETRY_SECONDS = 150
# Map areas a day's search still missed are asked again in the background, within the
# same search (not a second one: the day stays used), for up to FILL_IN_SECONDS after it
# ends: a round every FILL_IN_PAUSE_SECONDS, each with OVERPASS_RETRY_SECONDS. The
# businesses they find join the saved list as they arrive (fillin.py).
FILL_IN_SECONDS = 60 * 60
FILL_IN_PAUSE_SECONDS = 5 * 60
# A mirror that hasn't answered after this long is not waited out: the next one is
# asked as well, and the first good answer wins (a normal 30-mile query takes 10 to
# 40 seconds, and the slow mirror can still answer until the deadline).
OVERPASS_STAGGER_SECONDS = 25

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
# The free map data's answers are reused for less than a day: enough for the day's
# search to ask a missing area again and for its background filling in, while the next
# day's search of the same area asks the map servers again (what it finds as new is
# then really new since yesterday). The paid sources keep CACHE_TTL_SECONDS /
# YELP_CACHE_TTL_SECONDS (a repeat within the week costs nothing), and a search that
# reuses them says so (sources/paging.py REUSED).
OVERPASS_CACHE_TTL_SECONDS = 12 * 3600


# Off switches for whoever runs the site, read on every request: flipped on the Find
# leads page (Site switches, stored in the database, no restart) or, as a backup, set
# in Render > Environment (see docs/operator-runbook.md), where any of 1 / true / yes /
# on counts as set.
SEARCH_PAUSED_ENV = "LEADGEN_SEARCH_PAUSED"   # Find leads refuses to start
GOOGLE_OFF_ENV = "LEADGEN_GOOGLE_OFF"         # searches skip Google (no Google charges)
YELP_OFF_ENV = "LEADGEN_YELP_OFF"             # searches skip Yelp


def switched_on(name: str) -> bool:
    """True when the switch `name` is on: its environment variable is set to 1 / true /
    yes / on, or someone switched it on inside the site (switches.py)."""
    from . import switches  # it reads the database, which needs this module
    return switches.is_on(name)


# Why a running search makes no more paid lookups when the switches can't be read.
SWITCHES_UNREAD = ("the site's emergency switches couldn't be checked (the saved data wasn't "
                   "answering), so no more paid lookups were made")


def stop_reason(source: str | None = None) -> str | None:
    """Why a running search must stop now (plain words), or None.

    Checked between sources and before every paid call, so switching searching
    off (or Google / Yelp off) also stops a search that is already running. When the
    site's switches can't be read (the database stopped answering), a switch last
    seen on still counts as on, and no paid (Google / Yelp) lookup goes ahead while
    it isn't known to be off (SWITCHES_UNREAD); the free map data carries on.
    """
    from . import switches  # it reads the database, which needs this module
    unknown = False
    for name, applies, reason in (
            (SEARCH_PAUSED_ENV, True, "the administrator paused searching"),
            (GOOGLE_OFF_ENV, source == "google", "the administrator switched Google off"),
            (YELP_OFF_ENV, source == "yelp", "the administrator switched Yelp off")):
        if not applies:
            continue
        on = switches.checked(name)
        if on:
            return reason
        unknown = unknown or on is None
    if unknown and source in ("google", "yelp"):
        return SWITCHES_UNREAD
    return None
