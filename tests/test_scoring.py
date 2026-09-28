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


def test_shop_tag_beats_misleading_name():
    lead = score_lead(make("Sportsman's Warehouse", ["shop=outdoor"], source="osm"))
    assert lead.category_key == "specialty_retail"
    google = score_lead(make("Mattress Warehouse", ["furniture_store", "store"]))
    assert google.category_key == "specialty_retail"


def test_name_only_match_still_classifies():
    lead = score_lead(make("Fullstack Fulfillment", ["point_of_interest"]))
    assert lead.category_key == "distribution"


def test_unlisted_shop_falls_back_to_retail():
    lead = score_lead(make("Corner Florist", ["shop=florist"], source="osm"))
    assert lead.category_key == "retail"


def test_scrap_yard_is_recycling():
    lead = score_lead(make("Junkyard", ["industrial=auto_wrecker"], source="osm"))
    assert lead.category_key == "recycling"


def test_vets_and_clinics_are_not_hospitals():
    vet = score_lead(make("Cottonwood Animal Hospital", ["amenity=veterinary"], source="osm"))
    assert vet.category_key != "healthcare" and vet.score < 20
    heli = score_lead(make("LDS Hospital Heliport", ["aeroway=helipad"], source="osm"))
    assert heli.category_key != "healthcare"


def test_fast_food_dairy_is_food_service():
    dq = score_lead(make("Dairy Queen", ["amenity=fast_food"], source="osm"))
    assert dq.category_key == "food_service" and dq.score < 20


def test_short_name_keywords_are_whole_words():
    assert score_lead(make("Mallard Cove", [])).category_key != "venue"
    dental = score_lead(make("Gateway Dental", ["dentist"]))
    assert dental.category_key == "" and dental.score == 0


def test_retail_bakery_and_brewpub_are_not_plants():
    assert score_lead(make("Kneaders Bakery", ["bakery", "cafe", "store"])).category_key == "food_service"
    pub = score_lead(make("Bewilder Brewing", ["amenity=pub"], source="osm"))
    assert pub.category_key == "food_service"


def test_surname_brands_need_corroboration():
    assert not any("brand" in r for r in score_lead(make("Smith's Plumbing", ["plumber"])).reasons)
    assert not any("brand" in r for r in score_lead(make("Grown Ups Daycare", [])).reasons)
    assert not any("brand" in r for r in score_lead(make("The UPS Store", ["store"])).reasons)
    grocer = score_lead(make("Smith's Marketplace", ["supermarket"]))
    assert any("brand (smith's)" in r for r in grocer.reasons)
    tagged = score_lead(make("Smith's Fuel Center", ["brand=Smith's"], source="osm"))
    assert any("brand" in r for r in tagged.reasons)


def test_brand_variants_match():
    lead = score_lead(make("Intermountain Healthcare Distribution", ["point_of_interest"]))
    assert any("brand" in r for r in lead.reasons)
    assert any("brand" in r for r in score_lead(make("Swire Coca Cola", [])).reasons)


def test_google_search_query_is_weak_category_hint():
    lead = make("Utah Metal Works", ["point_of_interest", "establishment"])
    lead.search_terms = ["recycling center"]
    assert score_lead(lead).category_key == "recycling"
    shop = make("Mattress Warehouse", ["furniture_store", "store"])
    shop.search_terms = ["warehouse"]
    assert score_lead(shop).category_key == "specialty_retail"


def test_competitor_found_through_merged_name():
    lead = make("Utah Waste Equipment", [])
    lead.alt_names = ["Action Compaction"]
    assert score_lead(lead).lead_type == "Competitor"


def test_telling_name_beats_catch_all_industrial_tag():
    lead = score_lead(make("Wasatch Integrated Waste Management District", ["industrial=yes"],
                           source="osm"))
    assert lead.category_key == "recycling"
    plant = score_lead(make("Acme Waste Plant", ["industrial=food"], source="osm"))
    assert plant.category_key == "food_production"   # specific tag still wins


def test_brand_non_prospect_gets_no_bonus():
    heli = score_lead(make("Intermountain Medical Center Helipad", ["aeroway=helipad"], source="osm"))
    assert heli.score == 0
