from dataclasses import dataclass, field
from typing import Any

# A change that can still be undone, as the page gets it: {"id": ..., "until": epoch seconds}.
Undo = dict[str, Any]


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
    raw_categories: list[str] = field(default_factory=list)   # google types, "yelp:alias", osm "k=v"
    primary_category: str = ""       # source's own label, e.g. "Supermarket"
    rating_count: int | None = None          # Google review count
    yelp_reviews: int | None = None
    footprint_sqft: int | None = None
    business_status: str = ""
    map_url: str = ""
    search_terms: list[str] = field(default_factory=list)     # queries that found it
    alt_names: list[str] = field(default_factory=list)        # names/sites of merged duplicates

    # Filled in by scoring
    score: int = 0
    tier: str = ""
    category: str = ""
    category_key: str = ""
    lead_type: str = "Prospect"
    flags: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    matched_keywords: list[str] = field(default_factory=list)
    distance_miles: float | None = None
    sources: list[str] = field(default_factory=list)
    has_baler: str = ""              # "yes" / "no" as marked on the results page (marks.py)
    marked_by: str = ""              # who set that mark ("Your name" in their browser), if known
    uid: str = ""                    # the saved lead's id (saved.py)
    # The latest call (calls.py): when (epoch seconds), its result and notes, and how many.
    last_call_at: float | None = None
    call_outcome: str = ""
    call_notes: str = ""
    call_count: int = 0
    last_call_by: str = ""           # who logged the latest call, if known
    # When the latest call has no notes: the most recent notes an earlier call has, and when.
    earlier_notes: str = ""
    earlier_notes_at: float | None = None
    parts: list[dict[str, Any]] = field(default_factory=list)   # the source listings merged into this one
