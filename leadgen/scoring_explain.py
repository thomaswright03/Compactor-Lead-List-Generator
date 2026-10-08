"""A lead's explanation as a salesperson reads it on the Leads page and in the
downloads (plain_reasons)."""

import re
from collections.abc import Iterable

from .scoring_categories import GOOGLE, NAME, QUERY, RETAIL_BRAND, RULES, TAG, YELP

# ---- the explanation as a salesperson reads it
#
# lead.reasons is the scoring's own record, saved with each lead: "+35 Grocery /
# supermarket (by map tag): ...", and "category rule (own category): ..." naming the
# rule that decided, for whoever maintains the rules. The Leads page and the downloads
# show plain_reasons() of it instead, which also reads the explanations saved by earlier
# versions (saved leads are never rewritten).

# How a category matched, in a salesperson's words.
PLAIN_HOW = {GOOGLE: "from the Google listing", YELP: "from the Yelp listing", TAG: "from the map listing",
             NAME: "from its name", QUERY: "from the search that found it",
             RETAIL_BRAND: "from the store chain's name"}
_RULE_LINE = re.compile(r"category rule \(([^)]*)\)")
_HOW_PART = re.compile(r" \(by ([^)]+)\)")
_POINTS = re.compile(r"[+-]\d+ ")
# Lines of the record that read differently on the page and in the downloads.
_PLAIN_LINES = {"+0 category not on the high-volume list":
                "+0 not a kind of business that usually runs a compactor or baler"}
_PLAIN_WORDS = (("matches keyword(s): ", "matches the search words: "),)


def plain_reasons(reasons: Iterable[str]) -> list[str]:
    """A lead's explanation as the Leads page and the downloads show it: the points
    first ("+35 Grocery / supermarket (from the map listing): ..."), then the notes,
    starting with what the rule that decided its category means for a salesperson
    (Rule.note: "Only the building type or the business name suggests what it does —
    confirm before calling"), never the rule's name or the map's own terms."""
    notes_of = {rule.name: rule.note for rule in RULES}
    points: list[str] = []
    notes: list[str] = []
    for reason in reasons:
        rule = _RULE_LINE.match(reason)
        if rule:
            note = notes_of.get(rule.group(1), "")
            if note:
                notes.append(note)
            continue
        reason = _HOW_PART.sub(lambda m: f" ({PLAIN_HOW.get(m.group(1), 'from its listing')})", reason, count=1)
        reason = _PLAIN_LINES.get(reason, reason)
        for old, new in _PLAIN_WORDS:
            reason = reason.replace(old, new)
        if _POINTS.match(reason):
            points.append(reason)
        else:
            notes.append(reason[:1].upper() + reason[1:])
    return points + notes
