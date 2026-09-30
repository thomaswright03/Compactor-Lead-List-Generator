"""Extra search words choose what is searched; the saved score stays on the standard words."""

from leadgen import pipeline, saved, web
from leadgen.models import Lead
from leadgen.pipeline import SearchParams
from leadgen.sources import osm


def _map_lead(name, tags, sid=None, **kw):
    return Lead(name=name, lat=40.72, lon=-111.9, source="osm", source_id=sid or name,
                raw_categories=tags, **kw)


# ---- extra search words: the saved and shown score is the one the minimum applies to

def _pallet_search(monkeypatch, **params):
    monkeypatch.setattr(pipeline, "geocode", lambda location, key: (40.72, -111.9, "Salt Lake City"))
    monkeypatch.setattr(osm, "search", lambda *a, **k: (
        [_map_lead("Acme Pallet Co", ["building=industrial"])], []))
    return pipeline.run(SearchParams(source="osm", **params))


def test_search_words_do_not_change_the_score(monkeypatch):
    plain = _pallet_search(monkeypatch).leads[0]
    worded = _pallet_search(monkeypatch, keywords=["pallet"]).leads[0]
    assert worded.score == plain.score == 28
    assert not any("pallet" in r for r in worded.reasons)
    assert worded.matched_keywords == ["pallet"]          # still says which words matched


def test_the_minimum_score_applies_to_the_score_that_is_saved(monkeypatch):
    left_out = _pallet_search(monkeypatch, keywords=["pallet"], min_score=30)
    assert left_out.leads == [] and left_out.stats["below min score"] == 1
    kept = _pallet_search(monkeypatch, keywords=["pallet"], min_score=20)
    saved.save_search(kept.leads, ["pallet"])
    shown = saved.load()
    assert [(l.name, l.score) for l in shown] == [("Acme Pallet Co", 28)]
    assert all(l.score >= 20 for l in shown)
    assert shown[0].reasons == kept.leads[0].reasons       # "Why this score" agrees too


def test_only_matching_the_search_words_still_filters(monkeypatch):
    kept = _pallet_search(monkeypatch, keywords=["pallet"], only_keyword_matches=True)
    assert [l.name for l in kept.leads] == ["Acme Pallet Co"]
    gone = _pallet_search(monkeypatch, keywords=["lumber"], only_keyword_matches=True)
    assert gone.leads == [] and gone.stats["not matching keywords"] == 1


def test_the_form_says_what_search_words_do():
    page = web.create_app().test_client().get("/").get_data(as_text=True)
    assert "score higher" not in page
    assert "are always searched" in page
