"""The classifier's rules (scoring.RULES and scoring.VETOES): each has an example here,
the first rule that applies decides, and the lead's explanation names it. A new rule
needs an entry in its table and an example below (the last test checks there is one)."""

import copy
import re

import pytest

from leadgen import scoring
from leadgen.models import Lead
from leadgen.scoring import classify, score_lead


def make(name, cats=(), source="google", **kw):
    return Lead(name=name, lat=40.76, lon=-111.89, source=source, source_id=name,
                raw_categories=list(cats), **kw)


# (rule, the lead, the category it decides: a key, or None for no category)
RULE_EXAMPLES = [
    ("equipment name", make("Acme Compactor Service"), "equipment"),
    ("not-a-prospect name", make("Salt Lake City Police Impound Lot", ["landuse=industrial"], "osm"), None),
    ("utility structure", make("Pump Station 4", ["man_made=pumping_station"], "osm"), None),
    ("self-storage", make("Extra Space Storage", ["self_storage"]), None),
    ("retail chain", make("Harbor Freight Tools", ["building=industrial"], "osm"), "specialty_retail"),
    ("production listing", make("Uinta Brewing", ["brewery", "food_court"]), "food_production"),
    ("resort or mall name", make("Snowbird Ski & Summer Resort", ["building=yes", "shop=sports"], "osm"),
     "hospitality"),
    ("own category", make("Smith's Marketplace", ["supermarket"]), "grocery"),
    ("name beats type", make("Liberty Village Apartments", ["real_estate_agency"]), "multifamily"),
    ("not-a-prospect type", make("Animal Hospital of Murray", ["veterinary_care"]), None),
    ("shop", make("Liddiard Furniture", ["shop=mall"], "osm"), "retail"),
    ("not a plant", make("Pacificorp", ["building=industrial"], "osm"), None),
    ("catch-all tag or name", make("Acme Industries", ["building=industrial"], "osm"), "manufacturing"),
    ("search phrase", make("Acme", ["point_of_interest", "establishment"], search_terms=["warehouse"]),
     "distribution"),
]

# (veto, a lead it applies to, the same kind of lead it leaves alone, the category it holds back)
VETO_EXAMPLES = [
    ("not the campus itself", make("University Heights Condominiums"), make("Westminster University"),
     "education"),
    ("a small food shop", make("Day Dairy Barn"), make("Meadow Gold Dairy"), "food_production"),
    ("a shop named like a plant", make("Deseret Industries Thrift Store"), make("Acme Industries"),
     "manufacturing"),
    ("a shop mapped as a shopping centre", make("Liddiard Furniture", ["shop=mall"], "osm"),
     make("Fashion Place Mall", ["shop=mall"], "osm"), "venue"),
    ("a retail chain's name", make("Harbor Freight Tools"), make("Acme Freight"), "distribution"),
]


@pytest.mark.parametrize(("rule", "lead", "category"), RULE_EXAMPLES, ids=[r[0] for r in RULE_EXAMPLES])
def test_each_rule_decides_its_example(rule, lead, category):
    got = classify(lead)
    assert got.rule.name == rule
    assert (got.best[0].key if got.best else None) == category


@pytest.mark.parametrize(("veto", "applies", "spared", "category"), VETO_EXAMPLES,
                         ids=[v[0] for v in VETO_EXAMPLES])
def test_each_veto_holds_back_its_category(veto, applies, spared, category):
    assert category not in [c.key for c, _ in classify(applies).matched]
    assert category in [c.key for c, _ in classify(spared).matched]
    entry = next(v for v in scoring.VETOES if v.name == veto)
    assert category in entry.categories and entry.test(scoring.Facts(applies))


def test_the_first_rule_that_applies_decides():
    # An equipment dealer's name decides before its own (grocery) category does.
    got = classify(make("Baler Supermarket Supply", ["supermarket"]))
    assert got.rule.name == "equipment name" and got.best[0].key == "equipment"
    assert {c.key for c, _ in got.matched} == {"equipment", "grocery"}
    names = [r.name for r in scoring.RULES]
    assert names.index("equipment name") < names.index("own category")


def test_the_explanation_names_the_rule_that_decided():
    """The scoring's own record names the rule (for whoever maintains the rules); what a
    salesperson reads says what it means instead (test_explanations_are_in_plain_words)."""
    lead = score_lead(make("Smith's Marketplace", ["supermarket"]))
    assert lead.reasons[0].startswith("+35 Grocery / supermarket (by Google category)")
    assert "category rule (own category): its own Google category, Yelp category or map tag decides" \
        in lead.reasons
    storage = score_lead(make("Extra Space Storage", ["self_storage"]))
    assert any(r.startswith("category rule (self-storage)") for r in storage.reasons)
    # Nothing matched: no rule to name.
    assert not any("category rule" in r for r in score_lead(make("Nothing Here", ["point_of_interest"])).reasons)


# What a salesperson must never read: the classifier's own terms.
INTERNAL = re.compile(r"category rule|rule \(|catch-all|map tag|\btags?\b|\(by |matched by|high-volume list",
                      re.IGNORECASE)


def internal_words(text):
    """The classifier's own terms in `text` (rule names and texts too), or []."""
    found = INTERNAL.findall(text)
    for rule in scoring.RULES:
        found += [s for s in (f"({rule.name})", rule.says) if s in text]
    return found


@pytest.mark.parametrize(("rule", "lead", "category"), RULE_EXAMPLES, ids=[r[0] for r in RULE_EXAMPLES])
def test_explanations_are_in_plain_words(rule, lead, category):
    """Every rule's lead is explained in a salesperson's words: how its category is known
    ("from the map listing"), and what the rule means when that helps, never its name."""
    reasons = scoring.plain_reasons(score_lead(copy.deepcopy(lead)).reasons)
    assert reasons and not internal_words(" | ".join(reasons)), reasons
    note = next(r for r in scoring.RULES if r.name == rule).note
    assert (note in reasons) if note else len(reasons) == len([r for r in reasons if r[0] in "+-"]) + \
        sum(r.startswith("Also looks like") for r in reasons)


def test_explanations_saved_by_earlier_versions_read_plainly_too():
    saved_before = ["+28 Manufacturing / industrial (by map tag): Manufacturing sites compact scrap",
                    "category rule (catch-all tag or name): only a catch-all industrial tag or words in its "
                    "name say what it is", "+10 matches keyword(s): baler", "also looks like: Warehouse",
                    "category rule (a rule since renamed): internal words", "+0 category not on the high-volume list"]
    assert scoring.plain_reasons(saved_before) == [
        "+28 Manufacturing / industrial (from the map listing): Manufacturing sites compact scrap",
        "+10 matches the search words: baler",
        "+0 not a kind of business that usually runs a compactor or baler",
        "Only the building type or the business name suggests what it does — confirm before calling",
        "Also looks like: Warehouse"]


def test_a_listing_merged_from_google_and_the_map_is_classified():
    # A Google type beside a shop=mall tag used to raise while checking for a shop mapped as a mall.
    got = classify(make("Liddiard Furniture", ["store", "shop=mall"]))
    assert got.best[0].key == "retail"


# Map listings from a real search (their map tags as the map data had them), and what
# a salesperson would call them.
@pytest.mark.parametrize(("name", "tags", "category"), [
    # A shopping arcade whose building is mapped as apartments (580 Main Street, Park City).
    ("Galleria Mall", ["building=apartments", "building:levels=4"], "venue"),
    ("Galleria Mall", ["landuse=residential"], "venue"),
    ("Foothill Shopping Center", ["building=apartments"], "venue"),
    # The resort's office (Millrock Drive, Holladay), mapped as a sports shop.
    ("Snowbird Ski & Summer Resort", ["building=yes", "shop=sports"], "hospitality"),
    ("Deer Valley Resort", ["shop=outdoor"], "hospitality"),
    ("Alta Ski Area", ["shop=ticket"], "hospitality"),
    # Left as they were: apartments named after a mall, shops named after a resort, and
    # a mall or hotel whose own tag says so.
    ("Galleria Apartments", ["building=apartments"], "multifamily"),
    ("Mall View Apartments", ["building=apartments"], "multifamily"),
    ("Resort Sports", ["shop=sports"], "specialty_retail"),
    ("Snowbird Resort Ski Shop", ["shop=sports"], "specialty_retail"),
    ("Solitude Mountain Resort Store", ["shop=sports"], "specialty_retail"),
    ("Fashion Place Mall", ["shop=mall"], "venue"),
    ("Snowbird Lodge Resort", ["tourism=hotel"], "hospitality"),
])
def test_a_resort_or_a_mall_is_what_its_name_says(name, tags, category):
    got = classify(make(name, tags, "osm"))
    assert got.best[0].key == category, (got.best, got.rule.name)


def test_a_listing_says_more_than_a_resort_or_mall_name():
    # A Google or Yelp listing says what the business is: the name doesn't overrule it.
    assert classify(make("Snowbird Ski & Summer Resort", ["sporting_goods_store"])).best[0].key == \
        "specialty_retail"
    assert classify(make("Galleria Mall", ["apartment_complex"])).best[0].key == "multifamily"


def test_a_mall_and_a_resort_now_score_as_one():
    """The two listings the review found: a mall scores as a mall (28, tier C, not 15 as
    apartments), the resort's office as a resort (18, not 15 as a sports shop)."""
    mall = score_lead(make("Galleria Mall", ["building=apartments"], "osm"))
    resort = score_lead(make("Snowbird Ski & Summer Resort", ["building=yes", "shop=sports"], "osm"))
    assert (mall.score, mall.tier, mall.lead_type) == (28, "C", "Prospect")
    assert (resort.score, resort.tier) == (18, "D")
    assert "Its name says it's a resort or a shopping mall, although the map shows a shop or homes there" \
        in scoring.plain_reasons(resort.reasons)


def test_every_rule_and_veto_has_an_example():
    assert [r.name for r in scoring.RULES] == [e[0] for e in RULE_EXAMPLES]
    assert [v.name for v in scoring.VETOES] == [e[0] for e in VETO_EXAMPLES]
    assert len({r.name for r in scoring.RULES} | {scoring.NO_MATCH.name}) == len(scoring.RULES) + 1
