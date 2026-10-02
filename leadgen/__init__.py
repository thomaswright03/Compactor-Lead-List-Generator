"""Compactor Lead List Generator: find likely compactor/baler operators near a location."""

__version__ = "1.0.0"

# Every background thread the app starts is named with this prefix ("leadgen search
# 2026-10-02", "leadgen fill-in 2026-10-02", ...), so they can be told apart from the
# web server's own: the tests wait for all of them to end (tests/conftest.py).
THREAD_PREFIX = "leadgen "
