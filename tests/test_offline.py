"""The tests themselves never reach the internet or write where they are run."""

import socket

import pytest

# ---- the tests never reach the internet or write where they are run


def test_the_tests_cannot_reach_the_internet(tmp_path):
    with socket.socket() as sock, pytest.raises(OSError, match="may not reach the network"):
        sock.connect(("93.184.215.14", 443))
    import leadgen.http
    assert str(leadgen.http.CACHE_DIR) != ".cache"
