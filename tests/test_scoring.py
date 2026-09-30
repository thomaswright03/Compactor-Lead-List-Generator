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


def test_search_hint_ignored_for_real_but_unlisted_types():
    storage = make("Public Storage", ["storage", "point_of_interest", "establishment"])
    storage.search_terms = ["warehouse"]
    assert score_lead(storage).category_key == ""
    mover = make("Two Men and a Truck", ["moving_company"])
    mover.search_terms = ["logistics company"]
    assert score_lead(mover).score < 20


def test_manufacturer_type_and_wholesalers():
    assert score_lead(make("Acme Co", ["manufacturer", "point_of_interest"])).category_key == "manufacturing"
    platt = score_lead(make("Platt Electric Supply", ["wholesaler"]))
    assert platt.category_key == "wholesale"
    costco = score_lead(make("Costco", ["shop=wholesale", "brand=Costco"], source="osm"))
    assert costco.category_key == "wholesale" and costco.score >= 54


def test_courier_counter_is_not_a_warehouse():
    ups = score_lead(make("The UPS Store", ["courier_service", "store"]))
    assert ups.category_key != "distribution" and ups.score < 20


def test_specific_tag_beats_catch_all_industrial_building():
    for tag, expect in (("amenity=pub", "food_service"), ("amenity=veterinary", ""),
                        ("shop=furniture", "specialty_retail")):
        lead = score_lead(make("Red Rock Brewing Works", ["building=industrial", tag], source="osm"))
        assert lead.category_key == expect, tag


def test_production_brewery_and_bakery_vs_taproom():
    assert score_lead(make("Uinta Brewing", ["brewery"])).category_key == "food_production"
    assert score_lead(make("Bimbo Bakeries USA", ["bakery"])).category_key == "food_production"
    assert score_lead(make("Kneaders Bakery", ["bakery", "cafe", "store"])).category_key == "food_service"


def test_name_rescues_apartments_and_colleges_typed_as_agencies_or_schools():
    apt = score_lead(make("Liberty Village Apartments", ["real_estate_agency"]))
    assert apt.category_key == "multifamily"
    college = score_lead(make("Davis Technical College", ["school"]))
    assert college.category_key == "education"


def test_packing_movers_are_not_food_plants():
    assert score_lead(make("Wasatch Moving & Packing", [])).category_key != "food_production"


def test_keyword_variants():
    assert score_lead(make("Ace Disposal Services", [])).lead_type.startswith("Industry")
    assert score_lead(make("Acme Manufacturers", [])).category_key == "manufacturing"
    assert score_lead(make("Wasatch Recycler", [])).category_key == "recycling"


def test_more_surname_brands_need_corroboration():
    for name, types in (("Marshall's Plumbing & Heating", ["plumber"]),
                        ("Little Sprouts Daycare", []), ("Kraft Electric", ["electrician"]),
                        ("Michael's Jewelers", ["jewelry_store", "store"]),
                        ("Comfort at Home Furniture", ["furniture_store"])):
        lead = score_lead(make(name, types))
        assert not any("brand" in r for r in lead.reasons), name
    site = score_lead(make("Smith's Plumbing", ["plumber"], website="https://smithsplumbing.com"))
    assert not any("brand" in r for r in site.reasons)
    for name, types in (("Michaels", ["store"]), ("Marshalls", ["department_store"]),
                        ("Sprouts Farmers Market", ["grocery_store"]),
                        ("Kraft Heinz Foods", ["manufacturer"]), ("Pepsi-Cola Bottling Co", [])):
        assert any("brand" in r for r in score_lead(make(name, types)).reasons), name


def test_brand_pharmacy_and_gas_get_nothing():
    assert score_lead(make("Walmart Pharmacy", ["pharmacy", "drugstore", "health", "store"])).score < 20
    assert score_lead(make("Costco Gasoline", ["gas_station", "store"], rating_count=900)).score < 20


def test_reference_businesses_rank_where_they_should():
    """Businesses known to run a baler or compactor land in tiers A / B; ones known not
    to never do (tests/fixtures/scoring_reference.json)."""
    import json
    from pathlib import Path

    from leadgen import config
    from leadgen.models import Lead
    from leadgen.scoring import score_lead

    data = json.loads((Path(__file__).parent / "fixtures" / "scoring_reference.json").read_text())
    scored = []
    for item in data["businesses"]:
        lead = Lead(name=item["name"], lat=40.7, lon=-111.9, source=item["source"],
                    source_id=item["name"], raw_categories=item["raw_categories"],
                    rating_count=item.get("rating_count"), yelp_reviews=item.get("yelp_reviews"),
                    footprint_sqft=item.get("footprint_sqft"))
        score_lead(lead, config.DEFAULT_KEYWORDS)
        scored.append((item["has_equipment"], lead))
    have = [lead for has, lead in scored if has]
    lack = [lead for has, lead in scored if not has]
    strong = [lead.name for lead in have if lead.tier in ("A", "B")]
    assert len(strong) >= 0.75 * len(have), f"only {strong} reach tier A or B"
    assert not [lead.name for lead in lack if lead.tier in ("A", "B")]
    assert min(lead.score for lead in have) > max(lead.score for lead in lack)


def _moved_tiers(entries):
    """The confirmed entries whose tier moved the wrong way under the scoring now: a
    business marked Yes that dropped a tier, or one marked No that rose one."""
    from leadgen import config
    from leadgen.models import Lead
    from leadgen.reference import FACTS
    from leadgen.scoring import TIERS, score_lead

    rank = {tier: n for n, (_, tier) in enumerate(TIERS)}       # A = 0 ... D = 3
    moved = []
    for item in entries:
        lead = Lead(lat=40.7, lon=-111.9, source_id=item["name"],
                    **{k: item[k] for k in FACTS if k in item})
        score_lead(lead, config.DEFAULT_KEYWORDS)
        if (item["marked"] == "yes" and rank[lead.tier] > rank[item["tier"]]) or \
                (item["marked"] == "no" and rank[lead.tier] < rank[item["tier"]]):
            moved.append(f"{item['name']} (marked {item['marked']}): {item['tier']} -> {lead.tier}")
    return moved


def test_confirmed_businesses_keep_their_tier():
    """Businesses Arco marked Yes never drop a tier, and ones marked No never rise one,
    after a scoring change ("confirmed" in scoring_reference.json, copied from the
    site by `python -m leadgen reference` or its scoring check file download)."""
    import json
    from pathlib import Path

    data = json.loads((Path(__file__).parent / "fixtures" / "scoring_reference.json").read_text())
    moved = _moved_tiers(data.get("confirmed", []))
    assert not moved, moved


def test_the_confirmed_check_catches_a_weight_that_demotes_a_yes(monkeypatch):
    """What the check above does once real marks are in the file: copied from the
    saved list, the entries pass; lowering the weight a Yes depends on fails them."""
    from dataclasses import replace

    from leadgen import config, marks, reference, saved
    from leadgen.models import Lead
    from leadgen.scoring import score_lead

    lead = Lead(name="Harmons Grocery", lat=40.7, lon=-111.9, source="google", source_id="h1",
                raw_categories=["supermarket", "grocery_store"], rating_count=2400,
                phone="801-555-0100", address="1 Main St")
    score_lead(lead, config.DEFAULT_KEYWORDS)
    saved.save_search([lead])
    marks.set_mark(lead.uid, "yes", by="Sam")
    entries = reference.confirmed_entries(marks.apply(saved.load()))
    assert len(entries) == 1 and entries[0]["marked"] == "yes"
    assert "phone" not in entries[0] and "address" not in entries[0]
    assert _moved_tiers(entries) == []
    weakened = [replace(c, weight=1) if c.key == lead.category_key else c
                for c in config.CATEGORIES]
    monkeypatch.setattr(config, "CATEGORIES", weakened)
    monkeypatch.setattr(config, "CATEGORY_BY_KEY", {c.key: c for c in weakened})
    assert _moved_tiers(entries), "a demoted Yes must fail the check"


def test_the_site_offers_the_scoring_check_file():
    from leadgen import marks, saved, web
    from leadgen.models import Lead

    lead = Lead(name="Costco Wholesale", lat=40.7, lon=-111.9, source="google", source_id="c1",
                raw_categories=["warehouse_store"], phone="801-555-0101")
    saved.save_search([lead])
    marks.set_mark(lead.uid, "no", by="Sam")
    res = web.create_app().test_client().get("/download/scoring-reference.json")
    assert res.status_code == 200 and "attachment" in res.headers["Content-Disposition"]
    body = res.get_json()
    assert body["businesses"] and [e["name"] for e in body["confirmed"]] == ["Costco Wholesale"]
    assert "801" not in res.get_data(as_text=True)


def test_small_shops_with_a_production_word_are_not_plants():
    """A name like "Day Dairy Barn" (a small shop) never makes a food & beverage plant
    ("not_production" in scoring_reference.json); "Meadow Gold Dairy" still does."""
    import json
    from pathlib import Path

    from leadgen import config
    from leadgen.models import Lead
    from leadgen.scoring import score_lead

    data = json.loads((Path(__file__).parent / "fixtures" / "scoring_reference.json").read_text())
    assert any(item["name"] == "Day Dairy Barn" for item in data["not_production"])
    for item in data["not_production"]:
        lead = Lead(name=item["name"], lat=40.7, lon=-111.9, source=item["source"],
                    source_id=item["name"], raw_categories=item["raw_categories"])
        score_lead(lead, config.DEFAULT_KEYWORDS)
        assert lead.category_key != "food_production", (lead.name, lead.reasons)
    plant = Lead(name="Meadow Gold Dairy", lat=40.7, lon=-111.9, source="osm", source_id="m")
    score_lead(plant, config.DEFAULT_KEYWORDS)
    assert plant.category_key == "food_production"


def test_police_fire_impound_yards_and_lockers_are_not_prospects():
    """The reference's "not_prospect" places (a police impound lot, a fire department's
    training and logistics centre, a trailer yard, a parcel-locker brand) get no prospect
    category and fall below the default minimum score, whatever their tags say."""
    import json
    from pathlib import Path

    from leadgen import config
    from leadgen.models import Lead
    from leadgen.scoring import score_lead

    data = json.loads((Path(__file__).parent / "fixtures" / "scoring_reference.json").read_text())
    cases = [item for item in data["businesses"] if item.get("not_prospect")]
    assert len(cases) >= 4
    for item in cases:
        lead = Lead(name=item["name"], lat=40.7, lon=-111.9, source=item["source"],
                    source_id=item["name"], raw_categories=item["raw_categories"],
                    footprint_sqft=item.get("footprint_sqft"))
        score_lead(lead, config.DEFAULT_KEYWORDS)
        assert lead.category_key == "", (lead.name, lead.category)
        assert lead.score < config.DEFAULT_MIN_SCORE, (lead.name, lead.score)
    # A public yard still counts when its own tag says it is a recycling site.
    yard = Lead(name="County Fleet Maintenance Recycling Drop-off", lat=40.7, lon=-111.9,
                source="osm", source_id="n1", raw_categories=["amenity=recycling"])
    score_lead(yard, config.DEFAULT_KEYWORDS)
    assert yard.category_key == "recycling"


def test_small_industrial_sheds_and_utility_structures_are_not_prospects():
    """A pumping station, water well or substation, and a catch-all industrial building
    under 5,000 sq ft, get no prospect category (the reference's "6th East Well",
    "Pacificorp", "UTA Station"...); a real plant keeps its score."""
    import json
    from pathlib import Path

    from leadgen import config

    data = json.loads((Path(__file__).parent / "fixtures" / "scoring_reference.json").read_text())
    names = {item["name"] for item in data["businesses"] if item.get("not_prospect")}
    assert {"6th East Well", "Salt Lake City Corp", "Utah Power & Light Co", "Pacificorp",
            "UTA Station", "D04"} <= names
    well = score_lead(make("Pump", ["building=industrial", "man_made=pumping_station"], "osm",
                           footprint_sqft=300))
    assert well.category_key == "" and well.score < config.DEFAULT_MIN_SCORE
    big_well = score_lead(make("Station 4", ["building=industrial", "power=substation"], "osm",
                               footprint_sqft=60_000))
    assert big_well.category_key == ""
    city = score_lead(make("Maintenance Building", ["building=industrial",
                                                    "operator=Sandy City"], "osm",
                           footprint_sqft=20_000))
    assert city.category_key == ""
    plant = score_lead(make("Acme", ["building=industrial"], "osm", footprint_sqft=45_000))
    assert plant.category_key == "manufacturing" and plant.score >= 38
    unknown_size = score_lead(make("Acme", ["building=industrial"], "osm"))
    assert unknown_size.category_key == "manufacturing"
    factory = score_lead(make("Acme", ["building=industrial", "industrial=factory"], "osm",
                              footprint_sqft=3_000))
    assert factory.category_key == "manufacturing"
    named = score_lead(make("Wasatch Plastics", ["building=industrial"], "osm",
                            footprint_sqft=3_000))
    assert named.category_key == "manufacturing"
    recycling = score_lead(make("City Recycling", ["amenity=recycling", "man_made=storage_tank"],
                                "osm"))
    assert recycling.category_key == "recycling"


def test_names_that_hold_another_business_or_a_misleading_word():
    """The reference's "name_traps": a hotel named after the air base beside it gets no
    base brand bonus; a thrift store called "... Industries" is retail, not a plant."""
    import json
    from pathlib import Path

    from leadgen import config

    data = json.loads((Path(__file__).parent / "fixtures" / "scoring_reference.json").read_text())
    assert len(data["name_traps"]) >= 2
    for item in data["name_traps"]:
        lead = score_lead(make(item["name"], item["raw_categories"], item["source"]),
                          config.DEFAULT_KEYWORDS)
        assert lead.category_key == item["category"], (lead.name, lead.reasons)
        assert not [r for r in lead.reasons if item["never_reason"].lower() in r.lower()], lead.reasons
    # The brand still counts when the business is the brand.
    base = score_lead(make("Hill Air Force Base", ["landuse=military"], "osm"))
    assert any("hill air force base" in r for r in base.reasons)
    tagged = score_lead(make("Clearfield Store", ["shop=supermarket", "brand=Walmart"], "osm"))
    assert any("(walmart)" in r for r in tagged.reasons)
    near = score_lead(make("Motel near Costco", ["tourism=hotel"], "osm"))
    assert not any("costco" in r for r in near.reasons)
    other_brand = score_lead(make("Walmart Supercenter", ["shop=supermarket", "brand=Target"],
                                  "osm"))
    assert not any("(walmart)" in r for r in other_brand.reasons)
    plant = score_lead(make("Deseret Industries Manufacturing", ["building=industrial"], "osm"))
    assert plant.category_key == "manufacturing"
