"""A search's notes, numbers and progress in the Find leads page's words, for sales staff:
no URLs, error names or settings (the originals go to the log). plain_warning words a
search's notes, plain_details its numbers (the search history's "Details"), and
plain_progress the running search's message."""

import logging
import re
from typing import Any

from .. import config
from ..progress import LOCATE, MAP, MERGE, PAID, Step

log = logging.getLogger("leadgen.web")

# The search's numbers, as the page names them (the search history's "Details"), in the
# order they read as a funnel: found per source, within the radius, merged, left out,
# kept, then the kept leads by tier and what the search cost. "min score" goes into the
# low-score line's label.
DETAIL_LABELS = {
    "google raw results": "Businesses from Google",
    "yelp raw results": "Businesses from Yelp",
    "osm raw results": "Businesses from the free map data",
    # How much of the area the free map data covered: one line, in the words of the Find
    # page's status (fillin.coverage), never the count of parts asked again.
    "osm areas searched": "Free map data: area covered",
    "results in radius": "Listings within the radius",
    "duplicates merged": "Duplicate listings merged",
    "after dedupe": "Businesses after merging duplicates",
    "closed": "Left out: closed for good",
    "below min score": "Left out: score below the minimum",
    "not matching keywords": "Left out: not matching the search words",
    "over limit": "Left out: over the lead limit",
    "leads kept": "Leads kept",
    "competitors flagged": "Competitors among them (flagged)",
    "tier A": "Tier A leads",
    "tier B": "Tier B leads",
    "tier C": "Tier C leads",
    "tier D": "Tier D leads",
    "google requests": "Google lookups",
    "yelp requests": "Yelp calls",
    "seconds": "Took",
}
# Left-out lines that are only shown when something was left out that way.
_ONLY_IF_ANY = ("closed", "not matching keywords", "over limit", "duplicates merged")
# What a record holds that the page never shows: internal counts (how many parts of the
# map data were asked again), and what the coverage line already says.
_NOT_SHOWN = ("osm areas asked again", "osm areas filled in later")

_TECHNICAL = re.compile(r"https?://|HTTP \d{3}|Error\b|Exception|<html|\(\w+Error", re.I)
_SOURCE_WORDS = (("Google", "Google"), ("Yelp", "Yelp"), ("OpenStreetMap", "The map data service"),
                 ("Overpass", "The map data service"))


def plain_warning(text: str) -> str:
    """A search note in words for sales staff: no URLs, error names or cap settings.
    The original goes to the log."""
    who = next((name for word, name in _SOURCE_WORDS if word.lower() in text.lower()), "")
    if config.SWITCHES_UNREAD in text and text.startswith("Stopped early"):
        log.info("Search note: %s", text)
        return (f"{who or 'A paid source'} was stopped partway: {config.SWITCHES_UNREAD}. The "
                "businesses already found were kept.")
    if "the administrator" in text and text.startswith("Stopped early"):
        log.info("Search note: %s", text)
        return (f"{who or 'A source'} was stopped partway by the administrator; the businesses "
                "already found were kept.")
    if _TECHNICAL.search(text):
        log.info("Search note: %s", text)
        return (f"{who or 'One of the sources'} had a problem with part of the search; "
                "the businesses already found were kept.")
    if "request cap" in text or "--max-requests" in text:
        log.info("Search note: %s", text)
        return (f"{who or 'A paid source'} stopped at the limit on paid lookups for one "
                "search, so some searches were skipped.")
    if "No Google Places or Yelp API key" in text:
        return ("Google and Yelp aren't set up, so only the free map data was searched "
                "(it has fewer phone numbers).")
    return text


def plain_details(details: dict[str, object] | None, coverage: dict[str, Any] | None = None) -> list[list[object]]:
    """A search's numbers as [label, value] pairs in funnel order (older records keep
    their names), with how much of the area it covered as one line (coverage: the
    search's fillin.coverage). A list, because JSON objects lose their order on the way
    to the page."""
    details = {k: v for k, v in (details or {}).items() if k not in _NOT_SHOWN}
    if coverage:
        details["osm areas searched"] = coverage["short"]
    minimum = details.pop("min score", None)
    order = list(DETAIL_LABELS)
    out = []
    for key in sorted(details, key=lambda k: order.index(k) if k in order else len(order)):
        value = details[key]
        if key in _ONLY_IF_ANY and not value:
            continue
        label = DETAIL_LABELS.get(key, key)
        if key == "below min score" and minimum is not None:
            label = f"Left out: score below {minimum}"
        if key == "seconds" and isinstance(value, (int, float)):
            value = f"{round(value)} seconds" if value < 120 else f"{round(value / 60)} minutes"
        out.append([label, value])
    return out


# How each paid source names one of its calls, on the page as in the search's details.
_CALLS = {"google": ("Google", "lookups"), "yelp": ("Yelp", "calls")}
# What the map step is doing besides counting its areas (progress.Step's note).
_MAP_NOTES = {"split": " (some areas are being asked again in smaller pieces)",
              "retry": " (asking again for the areas the busy servers missed)"}


def plain_progress(msg: str) -> str:
    """The search's progress message in the page's words, made from the values a
    progress.Step carries (the text is for the command line); any other message is
    shown as it is."""
    if not isinstance(msg, Step):
        return str(msg)
    if msg.step == LOCATE:
        return "Finding the location"
    if msg.step == PAID:
        name, calls = _CALLS.get(msg.source, (msg.source.title(), "calls"))
        if not msg.note:
            return f"Searching {name}"
        return f"Searching {name} for {msg.note} ({msg.done:,} of up to {msg.total:,} {calls})"
    if msg.step == MAP:
        count = (f": {msg.done:,} of {msg.total:,} areas done" if msg.total > 1 else "")
        if msg.note == "stopped":
            return ("Stopping: the administrator paused searching. Keeping what was already "
                    f"found{f' ({count[2:]})' if count else ''}…")
        if msg.note == "another" and not count:
            # Which mirror is answering means nothing to a salesperson; only say one is being tried.
            return "Searching the free map data, trying another source…"
        return f"Searching the free map data{count}{_MAP_NOTES.get(msg.note, '')}…"
    if msg.step == MERGE:
        return ("Keeping the businesses within the radius" if msg.note == "radius"
                else "Merging duplicates and scoring")
    return "Saving leads"
