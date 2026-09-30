import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


@pytest.fixture(autouse=True)
def no_real_keys(monkeypatch, tmp_path):
    """Tests never see (or spend) real API keys, and never touch the real cache or
    daily usage counter."""
    for var in ("GOOGLE_PLACES_API_KEY", "YELP_API_KEY", "APP_PASSWORD", "DATABASE_URL", "RENDER",
                "LEADGEN_SEARCH_PAUSED", "LEADGEN_GOOGLE_OFF", "LEADGEN_YELP_OFF",
                "LEADGEN_SUPPORT_CONTACT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("leadgen.http.CACHE_DIR", tmp_path / "cache")
    # Problem reports (alerts.py) only in the tests about them, and never to a real webhook.
    monkeypatch.delenv("LEADGEN_ALERT_WEBHOOK", raising=False)
    monkeypatch.setattr("leadgen.alerts.ON", False)
    monkeypatch.setattr("leadgen.alerts.BACKGROUND", False)
    monkeypatch.setattr("leadgen.alerts._last", {})
    # Tests use a fresh SQLite file; LEADGEN_TEST_DATABASE_URL runs them on a
    # throwaway Postgres instead (its tables are emptied before every test).
    test_db = os.environ.get("LEADGEN_TEST_DATABASE_URL")
    if test_db:
        monkeypatch.setenv("DATABASE_URL", test_db)
        from leadgen import store
        with store.connect() as db:
            for table in store.TABLES:
                db.run(f"DELETE FROM {table}")
