"""Whether two listings' names and phone numbers say they are one business: names
cleaned of filler words, a town or a part of a site written into one of them, the site
a listing's name says it is part of, and how close two pins must be for each rule."""

import re
from difflib import SequenceMatcher
from functools import lru_cache

from . import places
from .scoring import normalize

_STOPWORDS = {"the", "inc", "llc", "co", "corp", "corporation", "company", "ltd", "store",
              "of", "and", "at", "utah", "ut", "slc"}
# Words that say what or where a place is, not who it is. Sharing only these
# ("Comfort Inn & Suites Airport" / "Fairfield Inn & Suites Airport") is not a match.
_GENERIC = {"apartments", "apartment", "apts", "main", "downtown", "recycling", "warehouse",
            "center", "centre", "hotel", "inn", "suites", "motel", "airport", "market",
            "plaza", "building", "office", "station", "north", "south", "east", "west",
            "salt", "lake", "city", "valley", "on", "by", "a", "in", "for", "street", "st",
            "avenue", "ave", "road", "rd", "park", "medical", "services", "service", "group"}
SAME_PLACE_MILES = 0.12      # ~200 m: two listings this close with similar names are one site
SAME_NAME_MILES = 0.2        # identical cleaned names
SAME_PHONE_MILES = 0.5       # the widest merge distance of all rules
# Words that name a part of a site, not the site ("Westpointe Center" / "Westpointe
# Campus", "Adagio Building A"), left out of the site's name.
_SITE_WORDS = {"campus", "center", "centre", "complex", "building", "buildings", "bldg",
               "unit", "units", "phase", "tower", "towers", "wing"}
# Words that name one building or part of a site once its site words are left out
# ("Intermountain Medical Center South Building" on the hospital's campus): a listing
# named like that inside the site's outline is part of the site (part_added).
_PART_WORDS = {"north", "south", "east", "west", "northeast", "northwest", "southeast", "southwest",
               "upper", "lower", "main", "annex", "pavilion"}


@lru_cache(maxsize=65536)
def clean_name(name: str) -> str:
    tokens = [t for t in normalize(name).split() if t not in _STOPWORDS and not t.isdigit()]
    return " ".join(tokens)


@lru_cache(maxsize=65536)
def _numbers(name: str) -> frozenset[str]:
    return frozenset(t for t in normalize(name).split() if t.isdigit())


@lru_cache(maxsize=65536)
def phone_numbers(phone: str) -> frozenset[str]:
    """Every 10-digit number in a tag like '+1 801-555-0100;+1 801-555-0199 ext. 2'."""
    nums = set()
    for part in re.split(r"[;,/]|\bor\b", phone or "", flags=re.IGNORECASE):
        part = re.split(r"(?:ext\.?|extension|x|#)\s*\d", part, maxsplit=1,
                        flags=re.IGNORECASE)[0]
        digits = re.sub(r"\D", "", part)
        if len(digits) >= 10:
            nums.add(digits[-10:])
    return frozenset(nums)


def names_match(a: str, b: str) -> bool:
    ca, cb = clean_name(a), clean_name(b)
    if not ca or not cb:
        return False
    if ca == cb:
        return True
    ta, tb = set(ca.split()), set(cb.split())
    small = ta if len(ta) <= len(tb) else tb
    if (ta <= tb or tb <= ta) and small - _GENERIC:
        return True
    if town_added(ca, cb):
        return True
    da = " ".join(t for t in ca.split() if t not in _GENERIC)
    db = " ".join(t for t in cb.split() if t not in _GENERIC)
    # Both the distinctive part and the full name must be close.
    return (bool(da and db) and SequenceMatcher(None, da, db).ratio() >= 0.85
            and SequenceMatcher(None, ca, cb).ratio() >= 0.85)


@lru_cache(maxsize=1)
def _towns() -> re.Pattern[str]:
    """Utah's town names (places.py), longest first, as whole words of a normalized name."""
    names = sorted({normalize(n) for n in places.town_names()} - {""}, key=len, reverse=True)
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(map(re.escape, names)) + r")(?![a-z0-9])")


@lru_cache(maxsize=65536)
def _without_towns(name: str) -> str:
    return " ".join(_towns().sub(" ", name).split())


def town_added(a: str, b: str) -> bool:
    """True when one cleaned name is the other with a town written into it ("smiths
    distribution center" / "smiths layton distribution"): only the second names a town,
    and without it both have the same two or more words that say who they are (words
    like "center" aside). Two names that each name a town ("layton auto parts" /
    "kaysville auto parts") are two towns' businesses, and one word left ("daniel
    construction" / "construction center") says too little."""
    plain_a, plain_b = _without_towns(a), _without_towns(b)
    if (plain_a == a) == (plain_b == b):
        return False
    who_a = {t for t in plain_a.split() if t not in _GENERIC}
    who_b = {t for t in plain_b.split() if t not in _GENERIC}
    return len(who_a) >= 2 and who_a == who_b


def part_added(a: str, b: str) -> bool:
    """True when one site name is the other with only words naming a part of a site
    added (_PART_WORDS: "intermountain medical south" / "intermountain medical"), and
    the shorter still says who it is (not only generic words)."""
    short, longer = sorted((a.split(), b.split()), key=len)
    extra = list(longer)
    for word in short:
        if word not in extra:
            return False
        extra.remove(word)
    return bool(extra) and all(w in _PART_WORDS for w in extra) and bool(set(short) - _GENERIC)


@lru_cache(maxsize=65536)
def site_name(name: str) -> str:
    """The name of the site a listing is part of: its cleaned name without building
    numbers or letters and without words like "campus" or "building" ("Shoreline Ridge
    825" -> "shoreline ridge"). "" when that leaves only generic words ("343
    Apartments", "Building 2"): such names say nothing about which site it is."""
    tokens = [t for t in normalize(name).split()
              if t not in _STOPWORDS and t not in _SITE_WORDS and len(t) > 1
              and not re.fullmatch(r"\d+[a-z]?", t)]
    return " ".join(tokens) if set(tokens) - _GENERIC else ""
