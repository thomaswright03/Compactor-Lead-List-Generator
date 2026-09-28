"""Merge duplicate listings (same business found by several queries or sources)."""

import re
from difflib import SequenceMatcher

from .geo import haversine_miles
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
    small = ta if len(ta) <= len(tb) else tb
    if (ta <= tb or tb <= ta) and small - _GENERIC:
        return True
    da = " ".join(t for t in ca.split() if t not in _GENERIC)
    db = " ".join(t for t in cb.split() if t not in _GENERIC)
    return bool(da and db) and SequenceMatcher(None, da, db).ratio() >= 0.85


def is_duplicate(a, b):
    if a.source == b.source and a.source_id == b.source_id:
        return True
    dist = haversine_miles(a.lat, a.lon, b.lat, b.lon)
    pa, pb = phone_digits(a.phone), phone_digits(b.phone)
    # Big sites: a store's entrance pin and its building outline can be ~300 m apart.
    ca = clean_name(a.name)
    if ca and ca == clean_name(b.name) and dist <= SAME_NAME_MILES:
        return True
    if pa and pb and pa != pb:
        return False          # different phone numbers: different businesses
    if dist <= SAME_PLACE_MILES and names_match(a.name, b.name):
        return True
    if not pa or pa != pb:
        return False
    return dist <= 0.05 or (dist <= SAME_PHONE_MILES and names_match(a.name, b.name))


def _resolve_status(group):
    """A place is closed only if no Google listing in the group says it is open."""
    google = {l.business_status for l in group if l.source == "google" and l.business_status}
    if "OPERATIONAL" in google:
        return "OPERATIONAL"
    if "CLOSED_PERMANENTLY" in google:
        return "CLOSED_PERMANENTLY"
    if "CLOSED_TEMPORARILY" in google:
        return "CLOSED_TEMPORARILY"
    return next((l.business_status for l in group if l.business_status), "")


def _merge(group):
    # Open listings first, then Google records (phone/website coverage), then the richest.
    group.sort(key=lambda l: (l.business_status == "CLOSED_PERMANENTLY", l.source != "google",
                              -sum(bool(x) for x in (l.phone, l.website, l.address, l.zip))))
    base = group[0]
    for other in group[1:]:
        for attr in ("address", "city", "state", "zip", "phone", "website", "primary_category",
                     "map_url"):
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
        # Keep merged names/sites so a competitor never hides behind another record.
        for extra in [other.name, other.website] + other.alt_names:
            if extra and extra not in (base.name, base.website) and extra not in base.alt_names:
                base.alt_names.append(extra)
    base.business_status = _resolve_status(group)
    base.sources = sorted({l.source for l in group})
    return base


def dedupe(leads):
    """Return merged leads.

    Two groups merge only when every member of one is a duplicate of every
    member of the other, so A~B and B~C never chain A and C together. A
    spatial grid keeps it fast on thousands of rows.
    """
    parent = list(range(len(leads)))
    members = {i: [i] for i in range(len(leads))}

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(ri, rj):
        parent[rj] = ri
        members[ri] += members.pop(rj)

    # Same record from the same source (found by several queries) is always one place.
    seen = {}
    for i, lead in enumerate(leads):
        key = (lead.source, lead.source_id)
        if key in seen:
            ri, rj = find(seen[key]), find(i)
            if ri != rj:
                union(ri, rj)
        else:
            seen[key] = i

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
                if j <= i:
                    continue
                ri, rj = find(i), find(j)
                if ri == rj or not is_duplicate(leads[i], leads[j]):
                    continue
                if all(is_duplicate(leads[x], leads[y]) for x in members[ri] for y in members[rj]):
                    union(ri, rj)

    groups = {}
    for i in range(len(leads)):
        groups.setdefault(find(i), []).append(leads[i])
    return [_merge(g) for g in groups.values()]
