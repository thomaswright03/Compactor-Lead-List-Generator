from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Lead:
    """One business location, as returned by a source and later scored."""

    name: str
    lat: float
    lon: float
    source: str                      # "google", "yelp" or "osm"
    source_id: str
    address: str = ""
    city: str = ""
    state: str = ""
    zip: str = ""
    phone: str = ""
    website: str = ""
    raw_categories: list = field(default_factory=list)   # google types, "yelp:alias", osm "k=v"
    primary_category: str = ""       # source's own label, e.g. "Supermarket"
    rating_count: Optional[int] = None          # Google review count
    yelp_reviews: Optional[int] = None
    footprint_sqft: Optional[int] = None
    business_status: str = ""
    map_url: str = ""
    search_terms: list = field(default_factory=list)     # queries that found it
    alt_names: list = field(default_factory=list)        # names/sites of merged duplicates

    # Filled in by scoring
    score: int = 0
    tier: str = ""
    category: str = ""
    category_key: str = ""
    lead_type: str = "Prospect"
    flags: list = field(default_factory=list)
    reasons: list = field(default_factory=list)
    matched_keywords: list = field(default_factory=list)
    distance_miles: Optional[float] = None
    sources: list = field(default_factory=list)
    has_baler: str = ""              # "yes" / "no" as marked on the results page (marks.py)
    uid: str = ""                    # the saved lead's id (saved.py)
    # The latest call (calls.py): when (epoch seconds), its result and notes, and how many.
    last_call_at: Optional[float] = None
    call_outcome: str = ""
    call_notes: str = ""
    call_count: int = 0
    parts: list = field(default_factory=list)   # the source listings merged into this one
