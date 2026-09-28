import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


@pytest.fixture(autouse=True)
def no_real_keys(monkeypatch):
    """Tests never see (or spend) real API keys from the environment."""
    for var in ("GOOGLE_PLACES_API_KEY", "YELP_API_KEY", "APP_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
