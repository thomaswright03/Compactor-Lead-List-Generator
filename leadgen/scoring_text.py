"""Text and tag helpers the scoring rules share: a name written one way (normalize),
whole-word matching, and a lead's Google types, Yelp categories and map tags."""

import re
from functools import lru_cache

from .models import Lead


@lru_cache(maxsize=65536)
def normalize(text: str) -> str:
    s = re.sub(r"[^a-z0-9 ]+", "", (text or "").lower().replace("&", " and "))
    return re.sub(r"\s+", " ", s).strip()


def compact(text: str) -> str:
    return normalize(text).replace(" ", "")


@lru_cache(maxsize=4096)
def _term_pattern(term: str, whole: bool) -> re.Pattern[str]:
    end = r"(?![a-z0-9])" if whole else ""
    return re.compile(rf"(?<![a-z0-9]){re.escape(term)}{end}")


def _contains_term(haystack: str, term: str, whole: bool = False) -> bool:
    """Match at a word start so 'ups' never matches 'supplies'.

    Prefix matches are allowed by default (user keyword 'recycl' matches
    'recycling'); whole=True also requires a word end ('mall' does not match
    'Mallard').
    """
    term = normalize(term)
    if not term:
        return False
    return _term_pattern(term, whole).search(haystack) is not None


def yelp_categories(lead: Lead) -> set[str]:
    """Yelp category aliases on a lead (stored as "yelp:alias")."""
    return {c[5:] for c in lead.raw_categories if c.startswith("yelp:")}


def google_types(lead: Lead) -> set[str]:
    """Google types on a lead: anything that is not an OSM "k=v" tag or a Yelp category."""
    return {c for c in lead.raw_categories if "=" not in c and not c.startswith("yelp:")}


def _osm_tag_matches(lead: Lead, key: str, value: str | None) -> bool:
    for c in lead.raw_categories:
        k, _, v = c.partition("=")
        if k == key and (value is None or value in v.split(";")):
            return True
    return False


def _split_hyphens(text: str) -> str:
    return normalize(re.sub(r"[-/]", " ", text or ""))
