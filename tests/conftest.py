import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


@pytest.fixture(autouse=True)
def no_real_keys(monkeypatch, tmp_path):
    """Tests never see (or spend) real API keys, and never touch the real cache or
    daily usage counter."""
    for var in ("GOOGLE_PLACES_API_KEY", "YELP_API_KEY", "APP_PASSWORD", "REDIS_URL", "RENDER"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("leadgen.http.CACHE_DIR", tmp_path / "cache")
