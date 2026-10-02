"""A failed request to Google, Yelp or a map server reaches the log (and the page) as one
short line: where it went, the status, how long it took and a short reason, never the
server's raw multi-line HTML error page."""

import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from leadgen import config, http
from leadgen.sources import SourceError, osm

# What an overloaded map mirror sends back (nginx behind a proxy).
GATEWAY_TIMEOUT = """<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Strict//EN"
 "http://www.w3.org/TR/xhtml1/DTD/xhtml1-strict.dtd">
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
<title>504 Gateway Time-out</title>
</head>
<body>
<center><h1>504 Gateway Time-out</h1></center>
<hr><center>nginx</center>
</body>
</html>
"""


@pytest.fixture
def mirror():
    """A server on this machine that answers every request with a 504 HTML page."""
    class Handler(BaseHTTPRequestHandler):
        def answer(self):
            body = GATEWAY_TIMEOUT.encode()
            self.send_response(504)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = answer

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/api/interpreter"
    server.shutdown()
    server.server_close()


def _one_line(text):
    assert "\n" not in text and "<" not in text and "html" not in text.lower(), text
    return text


def test_a_504_page_is_one_line_with_host_status_time_and_title(mirror, monkeypatch, caplog):
    monkeypatch.setattr(http.time, "sleep", lambda s: None)
    caplog.set_level(logging.INFO, logger="leadgen")
    with pytest.raises(http.HttpError) as caught:
        http.request_json("POST", mirror, data={"data": "[out:json];"}, use_cache=False, retries=2)
    error = _one_line(str(caught.value))
    host = mirror.split("/")[2]
    assert error.startswith(f"{host}/api/interpreter returned HTTP 504 after ")
    assert error.endswith("s: 504 Gateway Time-out") and caught.value.status == 504
    retried = [r.getMessage() for r in caplog.records if r.name == "leadgen.http"]
    assert len(retried) == 1 and host in retried[0] and "504" in retried[0]
    _one_line(retried[0])


def test_the_map_search_logs_a_failed_mirror_on_one_line(mirror, monkeypatch, caplog):
    monkeypatch.setattr(config, "OVERPASS_ENDPOINTS", [mirror])
    caplog.set_level(logging.WARNING, logger="leadgen")
    with pytest.raises(SourceError) as caught:
        osm._fetch("[out:json];node(1);out;", time.monotonic() + 30)
    _one_line(str(caught.value))
    failed = [r.getMessage() for r in caplog.records if "OpenStreetMap server failed" in r.getMessage()]
    assert len(failed) == 1 and mirror.split("/")[2] in failed[0] and "HTTP 504" in failed[0]
    _one_line(failed[0])


def test_the_reason_is_short_readable_and_keeps_api_error_codes():
    # An HTML page without a title: its words, without the markup.
    assert http.reason("<html><body>\n<p>Too   many\nrequests</p></body></html>") == "Too many requests"
    # A JSON error keeps the codes the sources look for (a used-up Yelp day, a bad key).
    yelp = '{\n  "error": {\n    "code": "ACCESS_LIMIT_REACHED",\n    "description": "Limit &amp; more"\n  }\n}'
    assert http.reason(yelp) == '{ "error": { "code": "ACCESS_LIMIT_REACHED", "description": "Limit & more" } }'
    # Keys echoed back in a URL are hidden; a long page is cut to one short line.
    assert "secret" not in http.reason("see https://x.test/?key=secret123 for details")
    long = http.reason("word " * 500)
    assert len(long) <= http.REASON_CHARS and long.endswith("...")
