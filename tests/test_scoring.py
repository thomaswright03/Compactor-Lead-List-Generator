from leadgen.models import Lead
from leadgen.scoring import score_lead


def make(name, cats=(), source="google", **kw):
    return Lead(name=name, lat=40.76, lon=-111.89, source=source, source_id=name,
                raw_categories=list(cats), **kw)


def test_grocery_brand_with_many_reviews_is_tier_a():
    lead = score_lead(make("Walmart Supercenter", ["supermarket", "store"], rating_count=3000))
    assert lead.category_key == "grocery"
    assert lead.tier == "A"
    assert any("high-volume brand" in r for r in lead.reasons)


def test_brand_needs_whole_word():
    lead = score_lead(make("Upscale Supplies", ["store"]))
    assert not any("brand" in r for r in lead.reasons)


def test_competitors_are_flagged_not_dropped():
    pro = score_lead(make("Pro Baler Inc"))
    act = score_lead(make("ACTION COMPACTION", website="https://actioncompaction.com"))
    assert pro.lead_type == "Competitor" and "COMPETITOR: Pro Baler" in pro.flags
    assert act.lead_type == "Competitor" and "COMPETITOR: Action Compaction" in act.flags


def test_competitor_detected_by_website_only():
    lead = score_lead(make("Utah Balers", website="https://www.probaler.com/"))
    assert lead.lead_type == "Competitor"


def test_own_company_is_flagged():
    lead = score_lead(make("Arco Compactor"))
    assert lead.lead_type == "Own company"


def test_equipment_name_is_industry_not_prospect():
    lead = score_lead(make("Wasatch Compactor Repair"))
    assert lead.lead_type.startswith("Industry")
    assert lead.flags


def test_osm_warehouse_with_large_footprint():
    lead = score_lead(make("Acme DC", ["building=warehouse"], source="osm", footprint_sqft=250_000))
    assert lead.category_key == "distribution"
    assert lead.score >= 45


def test_keywords_boost_and_are_listed():
    base = score_lead(make("Valley Recycling", ["recycling_center"]))
    boosted = score_lead(make("Valley Recycling", ["recycling_center"]), ["recycling", "baler"])
    assert boosted.score == base.score + 10
    assert boosted.matched_keywords == ["recycling"]


def test_unknown_business_scores_low():
    lead = score_lead(make("Joe's Barber", ["barber_shop"]))
    assert lead.score == 0 and lead.tier == "D"


def test_score_is_capped():
    lead = score_lead(make("Costco Wholesale", ["warehouse_store", "supermarket"], rating_count=9000),
                      ["costco", "wholesale", "grocery"])
    assert lead.score <= 100
