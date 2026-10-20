"""Whose Lead Finder this is: the company's name, its shop, its area, its extra search
words and its competitors, read in one place (current()).

On AARCO's own site (sign-up mode off, signup_mode.py) they are AARCO's, from config.py,
read every time (so a test that changes a config setting sees it). In sign-up mode they
are the company's own, saved with its account (accounts.py) and set for each web request
(web/accounts.py) and for the searches it starts (store.carry); the site is then just
"Lead Finder", with the company's name where AARCO's would be. Scoring is the same for
every company (the baler and compactor rules); only which listing is the company's own
and which are its competitors come from here.
"""

import contextvars
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from . import config, signup_mode
from .scoring_text import compact, normalize

# The site's name in sign-up mode (no company's name before it).
PRODUCT_NAME = "Lead Finder"


@dataclass(frozen=True)
class Company:
    """A company that signed up, as its account row holds it (accounts.py)."""
    id: str
    name: str
    short_name: str
    address: str = ""
    lat: float | None = None
    lon: float | None = None
    radius_miles: float = config.DEFAULT_RADIUS_MILES
    search_words: tuple[str, ...] = ()
    competitors: tuple[str, ...] = ()
    created_at: float = 0.0
    plan: str = "trial"
    status: str = "trialing"
    trial_ends_at: float | None = None
    stripe_customer_id: str | None = None
    stripe_subscription_id: str | None = None

    @property
    def located(self) -> bool:
        """True once the shop's address has been looked up (the setup's first step)."""
        return self.lat is not None and self.lon is not None


@dataclass(frozen=True)
class Settings:
    """What the pages, the search and the scoring read about whose site this is."""
    name: str                                   # the company's name (config.OWN_COMPANY)
    short_name: str                             # on a phone, beside the Menu button
    site_name: str                              # every page's title
    address: str                                # the shop (config.OWN_ADDRESS)
    shop: tuple[float, float]                   # its coordinates (config.OWN_COORDS)
    center: tuple[float, float]                 # the area's centre (config.SERVICE_CENTER)
    area_miles: float                           # the area around it (config.SERVICE_AREA_MILES)
    radius: float                               # the search form's miles (config.DEFAULT_RADIUS_MILES)
    keywords: list[str]                         # the standard search words, always searched
    competitors: dict[str, list[str]]           # name -> how it is written (config.COMPETITORS)
    self_aliases: list[str]                     # how the company's own listing is written
    parent_company: str = ""                    # AARCO's parent company (config.PARENT_*)
    parent_aliases: list[str] = field(default_factory=list)
    parent_domains: list[str] = field(default_factory=list)
    parent_addresses: list[str] = field(default_factory=list)
    company: Company | None = None              # sign-up mode: the account it came from

    @property
    def own_label(self) -> str:
        """The shop as the search page names it: "AARCO Waste & Disposal, 876 Fortune Rd ..."."""
        return f"{self.name}, {self.address}" if self.address else self.name


# The company of this web request or search (sign-up mode); None on AARCO's own site.
_current: contextvars.ContextVar[Company | None] = contextvars.ContextVar("company_settings", default=None)


def use(company: Company | None) -> contextvars.Token[Company | None]:
    """Read this company's settings from now on in this thread (store.set_company sets
    whose data is reached; web/accounts.py sets both)."""
    return _current.set(company)


def reset(token: contextvars.Token[Company | None]) -> None:
    _current.reset(token)


def aliases(name: str) -> list[str]:
    """How a name may be written in a listing: as words, and as one word ("Pro Baler",
    "probaler"), matched as scoring_flags does."""
    return list(dict.fromkeys(a for a in (normalize(name), compact(name)) if a))


def _aarco() -> Settings:
    """AARCO's own site: config.py's settings, as they are now."""
    return Settings(
        name=config.OWN_COMPANY, short_name=config.OWN_COMPANY_SHORT, site_name=config.SITE_NAME,
        address=config.OWN_ADDRESS, shop=config.OWN_COORDS, center=config.SERVICE_CENTER,
        area_miles=config.SERVICE_AREA_MILES, radius=config.DEFAULT_RADIUS_MILES,
        keywords=list(config.DEFAULT_KEYWORDS), competitors=dict(config.COMPETITORS),
        self_aliases=list(config.SELF_ALIASES), parent_company=config.PARENT_COMPANY,
        parent_aliases=list(config.PARENT_ALIASES), parent_domains=list(config.PARENT_DOMAINS),
        parent_addresses=list(config.PARENT_ADDRESSES))


@lru_cache(maxsize=256)
def settings_of(company: Company) -> Settings:
    """A signed-up company's settings (worked out once for each version of its details).
    Before its shop is set up its area is centred on config's default (the setup asks for
    the shop first)."""
    shop = (company.lat, company.lon) if company.lat is not None and company.lon is not None \
        else config.OWN_COORDS
    names = [company.name, company.short_name]
    return Settings(
        name=company.name, short_name=company.short_name, site_name=PRODUCT_NAME,
        address=company.address, shop=shop, center=shop, area_miles=company.radius_miles,
        radius=company.radius_miles,
        keywords=list(dict.fromkeys([*config.DEFAULT_KEYWORDS, *company.search_words])),
        competitors={c: aliases(c) for c in company.competitors},
        self_aliases=list(dict.fromkeys(a for n in names for a in aliases(n))),
        company=company)


def current() -> Settings:
    """The settings of whose site this is: the request's (or search's) company in sign-up
    mode (the product's own name outside one), else AARCO's (config.py)."""
    company = _current.get()
    if signup_mode.on():
        return settings_of(company) if company is not None else _no_company()
    return _aarco()


def _no_company() -> Settings:
    """Sign-up mode outside any company (the public pages, a visitor not logged in): the
    product's own name, never AARCO's."""
    return Settings(
        name=PRODUCT_NAME, short_name=PRODUCT_NAME, site_name=PRODUCT_NAME, address="",
        shop=config.OWN_COORDS, center=config.SERVICE_CENTER, area_miles=config.SERVICE_AREA_MILES,
        radius=config.DEFAULT_RADIUS_MILES, keywords=list(config.DEFAULT_KEYWORDS), competitors={},
        self_aliases=[])


def page_names() -> dict[str, Any]:
    """The names every page shows (web/__init__.py page_globals)."""
    now = current()
    return {"company": now.name, "company_short": now.short_name, "site_name": now.site_name}
