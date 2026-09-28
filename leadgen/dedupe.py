"""Merge duplicate listings (same business found by several queries or sources)."""

import re
from difflib import SequenceMatcher

from .geo import haversine_miles
from .scoring import normalize

_STOPWORDS = {"the", "inc", "llc", "co", "corp", "corporation", "company", "ltd", "store",
              "of", "and", "at", "utah", "ut", "slc"}
SAME_PLACE_MILES = 0.12      # ~200 m: two listings this close with similar names are one site
SAME_PHONE_MILES = 0.5


def clean_name(name):
    tokens = [t for t in normalize(name).split() if t not in _STOPWORDS and not t.isdigit()]
    return " ".join(tokens)


def phone_digits(phone):
    digits = re.sub(r"\D", "", phone or "")
    return digits[-10:] if len(digits) >= 10 else ""


def names_match(a, b):
    ca, cb = clean_name(a), clean_name(b)
    if not ca or not cb:
        return False
    if ca == cb:
        return True
    ta, tb = set(ca.split()), set(cb.split())
    if ta <= tb or tb <= ta:
        return True
    return SequenceMatcher(None, ca, cb).ratio() >= 0.85


def is_duplicate(a, b):
    if a.source == b.source and a.source_id == b.source_id:
        return True
    dist = haversine_miles(a.lat, a.lon, b.lat, b.lon)
    if dist <= SAME_PLACE_MILES and names_match(a.name, b.name):
        return True
    pa, pb = phone_digits(a.phone), phone_digits(b.phone)
    if not pa or pa != pb:
        return False
    return dist <= 0.05 or (dist <= SAME_PHONE_MILES and names_match(a.name, b.name))


def _merge(group):
    # Prefer Google records (phone/website coverage), then the richest record.
    group.sort(key=lambda l: (l.source != "google",
                              -sum(bool(x) for x in (l.phone, l.website, l.address, l.zip))))
    base = group[0]
    for other in group[1:]:
        for attr in ("address", "city", "state", "zip", "phone", "website", "primary_category",
                     "business_status"):
            if not getattr(base, attr) and getattr(other, attr):
                setattr(base, attr, getattr(other, attr))
        if other.rating_count and (base.rating_count or 0) < other.rating_count:
            base.rating_count = other.rating_count
        if other.footprint_sqft and (base.footprint_sqft or 0) < other.footprint_sqft:
            base.footprint_sqft = other.footprint_sqft
        for c in other.raw_categories:
            if c not in base.raw_categories:
                base.raw_categories.append(c)
        for t in other.search_terms:
            if t not in base.search_terms:
                base.search_terms.append(t)
    base.sources = sorted({l.source for l in group})
    extra_urls = [l.map_url for l in group[1:] if l.map_url and l.map_url != base.map_url]
    if extra_urls and not base.map_url:
        base.map_url = extra_urls[0]
    return base


def dedupe(leads):
    """Return merged leads. Uses a spatial grid so it stays fast on thousands of rows."""
    parent = list(range(len(leads)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    buckets = {}
    for i, lead in enumerate(leads):
        buckets.setdefault((round(lead.lat, 2), round(lead.lon, 2)), []).append(i)

    for (bx, by), idxs in buckets.items():
        neighbors = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                neighbors += buckets.get((round(bx + dx * 0.01, 2), round(by + dy * 0.01, 2)), [])
        for i in idxs:
            for j in neighbors:
                if j <= i or find(i) == find(j):
                    continue
                if is_duplicate(leads[i], leads[j]):
                    parent[find(j)] = find(i)

    # Same source id can be far apart only if the source moved it; catch exact-id dupes too.
    seen = {}
    for i, lead in enumerate(leads):
        key = (lead.source, lead.source_id)
        if key in seen:
            parent[find(i)] = find(seen[key])
        else:
            seen[key] = i

    groups = {}
    for i in range(len(leads)):
        groups.setdefault(find(i), []).append(leads[i])
    return [_merge(g) for g in groups.values()]
