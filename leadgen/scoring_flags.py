"""AARCO's own listings, its parent company's and its competitors': how each is
recognised (its names, website or a facility's address: config.SELF_ALIASES,
config.PARENT_*, config.COMPETITORS) and the type and flag it gets, as a search scores
it and as a row saved under earlier rules is shown. In sign-up mode (signup_mode.py) the
own listing and the competitors are the company's own (company.current()); a company that
signed up has no parent company."""

import re
from collections.abc import Sequence
from functools import lru_cache

from . import company, config, places
from .models import Lead
from .scoring_text import _split_hyphens, compact, normalize

# The flag on AARCO's own listing and on its parent company's (config.PARENT_*); rows
# saved under an earlier name show it as it is now (current_own_flag, saved.py). In
# sign-up mode the company's own listing is flagged with its name (own_flag).
OWN_FLAG_START = "OWN COMPANY: "
OWN_FLAG = OWN_FLAG_START + config.OWN_COMPANY
PARENT_FLAG = f"{OWN_FLAG_START}AARCO's parent company ({config.PARENT_COMPANY})"
OWN_TYPE = "Own company"
PROSPECT_TYPE = "Prospect"
# The flag on a competitor's listing (config.COMPETITORS): "COMPETITOR: Pro Baler".
COMPETITOR_FLAG_START = "COMPETITOR: "
INDUSTRY_FLAG = "Possible competitor or partner (equipment / hauler)"


def own_flag() -> str:
    """The flag on the company's own listing: OWN_FLAG on AARCO's site, else with the
    signed-up company's name."""
    return OWN_FLAG_START + company.current().name


# ---- a listing's names and website against a list of names (config.SELF_ALIASES,
# config.COMPETITORS, config.PARENT_ALIASES): all three match the same way


@lru_cache(maxsize=16)
def _alias_patterns(aliases: tuple[str, ...]) -> tuple[re.Pattern[str], re.Pattern[str]]:
    """(any alias as whole words, an "s" after it allowed; any alias written as one word
    opening the text)."""
    words = "|".join(re.escape(normalize(a)) for a in aliases if normalize(a))
    joined = "|".join(re.escape(compact(a)) for a in aliases if compact(a))
    return re.compile(rf"(?<![a-z0-9])(?:{words})s?(?![a-z0-9])"), re.compile(rf"^(?:{joined})")


def _alias_named(name: str, aliases: Sequence[str]) -> bool:
    """True when a name holds one of aliases as whole words ("Pro Baler Inc", "Pro-Baler",
    "AARCO Compactors & Balers") or written as one word opening it ("ProBaler",
    "AarcoWaste"), never inside another word ("Repro Balers", "Marco Compactor Repair")."""
    if not aliases:
        return False
    words, joined = _alias_patterns(tuple(aliases))
    plain = normalize(name)
    return bool(words.search(plain) or joined.search(plain.replace(" ", ""))
                or (("-" in name or "/" in name) and words.search(_split_hyphens(name))))


def _alias_site(website: str, aliases: Sequence[str]) -> bool:
    """True when a website's address holds one of aliases as whole words between its dots,
    dashes and slashes ("pro-baler.com"), or a part of it opens with one written as one
    word ("probaler.com", "aarcowastedisposal.com"), never inside another ("reprobalers.com")."""
    if not aliases:
        return False
    words, joined = _alias_patterns(tuple(aliases))
    parts = re.split(r"[^a-z0-9]+", re.sub(r"^(https?://)?(www\d?\.)?", "", website.strip().lower()))
    return bool(words.search(" ".join(parts)) or any(joined.match(p) for p in parts if p))


def _holds_alias(lead: Lead, aliases: Sequence[str]) -> bool:
    """True when the lead's name, website or another name it goes by holds one of aliases
    (_alias_named, _alias_site)."""
    return (any(_alias_named(x, aliases) for x in [lead.name, *lead.alt_names] if x)
            or any(_alias_site(x, aliases) for x in [lead.website, *lead.alt_names] if x))


def _competitor(lead: Lead) -> str | None:
    """The competitor (config.COMPETITORS, or the company's own list) whose name or website
    the lead holds, or None."""
    competitors = company.current().competitors
    return next((name for name, aliases in competitors.items() if _holds_alias(lead, aliases)), None)


def _is_self(lead: Lead) -> bool:
    """True for AARCO's own listing: its names or website hold one of config.SELF_ALIASES
    (in sign-up mode, the company's own names)."""
    return _holds_alias(lead, company.current().self_aliases)


def _own_or_competitor(lead: Lead) -> tuple[str, str] | None:
    """(type, flag) for a competitor's listing or AARCO's own, else None."""
    competitor = _competitor(lead)
    if competitor:
        return "Competitor", COMPETITOR_FLAG_START + competitor
    if _is_self(lead):
        return OWN_TYPE, own_flag()
    return None


def _type_flags(lead: Lead) -> list[str]:
    """A competitor's, AARCO's own (its parent company's) or an industry listing's flag,
    with the lead's type set to match."""
    own_or_competitor = _own_or_competitor(lead)
    if own_or_competitor:
        lead.lead_type, flag = own_or_competitor
        return [flag]
    if parent_company(lead):
        lead.lead_type = OWN_TYPE
        return [PARENT_FLAG]
    if lead.lead_type.startswith("Industry"):
        return [INDUSTRY_FLAG]
    return []


# ---- AARCO's parent company (config.PARENT_*): its listings are AARCO's own

# Street words written short, so "1240 South Wallace Road" = "1240 S Wallace Rd".
_STREET_SHORT = {"north": "n", "south": "s", "east": "e", "west": "w", "road": "rd", "street": "st",
                 "avenue": "ave", "av": "ave", "drive": "dr", "boulevard": "blvd", "lane": "ln",
                 "court": "ct", "circle": "cir", "highway": "hwy", "parkway": "pkwy", "place": "pl"}


def _street_words(street: str) -> list[str]:
    return [_STREET_SHORT.get(w, w) for w in normalize(street).split()]


@lru_cache(maxsize=4)
def _parent_facilities(addresses: tuple[str, ...]) -> tuple[tuple[list[str], str], ...]:
    """config.PARENT_ADDRESSES as (street words, town as places.tidy_town writes it)."""
    out = []
    for address in addresses:
        street, _, town = address.partition(",")
        if _street_words(street):
            out.append((_street_words(street), places.tidy_town(town)))
    return tuple(out)


def _parent_named(name: str) -> bool:
    """True when a name holds one of config.PARENT_ALIASES as whole words ("Ace
    Intermountain Recycling Center", "ACE-Disposal", "AceDisposal"), never inside other
    words or as a bare "Ace" ("Ace Hardware", "Grace Recycling")."""
    return _alias_named(name, company.current().parent_aliases)


def _parent_site(website: str) -> bool:
    domains = company.current().parent_domains
    if not any(d in website.lower() for d in domains):
        return False
    host = re.sub(r"^(https?://)?(www\d?\.)?", "", website.strip().lower()).split("/")[0].split(":")[0]
    return any(host == d or host.endswith("." + d) for d in domains)


def _parent_address(lead: Lead) -> bool:
    """True when the lead's street address is one of config.PARENT_ADDRESSES (a suite or
    unit after it allowed) and the town it lists, if any, is that address's town."""
    facilities = _parent_facilities(tuple(company.current().parent_addresses))
    if not lead.address or not any(lead.address.lstrip().lower().startswith(street[0]) for street, _ in facilities):
        return False            # not even its house number: most listings stop here
    words = _street_words(lead.address)
    towns = [where for street, where in facilities if words[:len(street)] == street]
    if not towns:
        return False
    town = places.listed_town(lead)
    return not town or any(not where or town == where for where in towns)


def parent_company(lead: Lead) -> bool:
    """True for a listing of AARCO's parent company (config.PARENT_*): by its name or
    another name it goes by, its website, or one of its facilities' addresses."""
    return (any(_parent_named(x) for x in [lead.name, *lead.alt_names] if x)
            or any(_parent_site(x) for x in [lead.website, *lead.alt_names] if x)
            or _parent_address(lead))


def mark_parent_company(lead: Lead) -> Lead:
    """A listing of AARCO's parent company is AARCO's own: never a prospect, flagged
    PARENT_FLAG. score_lead does it, and so does showing a row saved before the parent
    company was recognised (saved.py; the saved row is unchanged). A competitor, or a
    listing already marked as AARCO's own, is left as it is."""
    if lead.lead_type in ("Competitor", OWN_TYPE) or not parent_company(lead):
        return lead
    lead.lead_type = OWN_TYPE
    lead.flags = [*(f for f in lead.flags if f != INDUSTRY_FLAG), PARENT_FLAG]
    return lead


# ---- a row saved under earlier rules, shown as the rules read it now (saved._ready)


def current_own_flag(flag: str) -> str:
    """An own-company flag as it reads now: one saved under an earlier name of AARCO
    ("OWN COMPANY: AARCO Compactors & Balers", "OWN COMPANY: AARCO Compactor",
    "OWN COMPANY: Arco Compactor") or of the parent
    company reads with the names config has now; any other flag is returned as it is."""
    if not flag.startswith(OWN_FLAG_START):
        return flag
    return PARENT_FLAG if "parent company" in flag.lower() else own_flag()


def current_type_flags(lead: Lead) -> Lead:
    """A saved competitor's or own-company flag (OWN_FLAG, not the parent company's) as
    the names match now: a row flagged before the names had to match as whole words
    (SCORING_VERSION 1: "Marco Compactor Repair" read as AARCO's own, "Repro Balers" as
    Pro Baler) is shown with the type its category gives and without the flag, as a
    search now scores it. saved.py does it after current_own_flag; the saved row is
    unchanged."""
    own = own_flag()
    old = {f for f in lead.flags if f == own or f.startswith(COMPETITOR_FLAG_START)}
    if not old:
        return lead
    now = _own_or_competitor(lead)
    if now and now[1] in old:
        return lead
    if now:
        lead.lead_type, flag = now
        flags = [flag]
    else:
        category = config.CATEGORY_BY_KEY.get(lead.category_key)
        lead.lead_type = category.lead_type if category else PROSPECT_TYPE
        flags = [INDUSTRY_FLAG] if lead.lead_type.startswith("Industry") else []
    at = next(i for i, f in enumerate(lead.flags) if f in old)
    kept = [f for f in lead.flags if f not in old]
    lead.flags = [*kept[:at], *flags, *kept[at:]]
    return lead
