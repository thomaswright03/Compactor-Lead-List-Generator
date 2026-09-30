"""A university's units and neighbours are not campus prospects; campuses still are."""

import json
from pathlib import Path

import pytest

from leadgen import config
from leadgen.models import Lead
from leadgen.scoring import score_lead


def _map_lead(name, tags, sid=None, **kw):
    return Lead(name=name, lat=40.72, lon=-111.9, source="osm", source_id=sid or name,
                raw_categories=tags, **kw)


# ---- a university's units and neighbours are not campus prospects

REFERENCE = json.loads((Path(__file__).parent / "fixtures" / "scoring_reference.json").read_text())


@pytest.mark.parametrize("item", REFERENCE["not_campus"], ids=lambda i: i["name"])
def test_campus_units_and_neighbours_are_not_campus_prospects(item):
    lead = _map_lead(item["name"], item["raw_categories"])
    score_lead(lead, config.DEFAULT_KEYWORDS)
    assert lead.category_key != "education" and lead.tier == "D"


@pytest.mark.parametrize("name,tags", [("University of Utah", ["amenity=university"]),
                                       ("Salt Lake Community College", []),
                                       ("College of Eastern Utah", []),
                                       ("Westminster College", ["university"])])
def test_campuses_are_still_campus_prospects(name, tags):
    lead = _map_lead(name, tags)
    score_lead(lead, config.DEFAULT_KEYWORDS)
    assert lead.category_key == "education" and lead.tier == "C"
