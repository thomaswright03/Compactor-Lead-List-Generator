"""The pages in a real browser: marks, undo, calls, stats and downloads.

Needs Playwright and its Chromium (pip install playwright; playwright install
chromium). Skipped when they are missing. LEADGEN_CHROMIUM can point at a
Chromium binary to use instead of Playwright's own.
"""

import os
import threading

import pytest

from leadgen import config, saved, web
from leadgen.models import Lead
from leadgen.scoring import score_lead

sync_api = pytest.importorskip("playwright.sync_api")


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        try:
            chromium = p.chromium.launch(executable_path=os.environ.get("LEADGEN_CHROMIUM") or None)
        except Exception as exc:
            pytest.skip(f"Chromium is not available: {exc}")
        yield chromium
        chromium.close()


@pytest.fixture(scope="module")
def site():
    from werkzeug.serving import make_server
    server = make_server("127.0.0.1", 0, web.create_app(), threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/"
    server.shutdown()


@pytest.fixture
def page(browser, site):
    leads = []
    for i, (name, cats) in enumerate([("Smith's Marketplace", ["yelp:grocery"]),
                                      ("Costco Wholesale", ["yelp:wholesale_stores"]),
                                      ("Hampton Inn", ["yelp:hotels"])]):
        lead = Lead(name=name, lat=40.72 + i * 0.01, lon=-111.9, source="yelp", source_id=f"y{i}",
                    phone=f"(801) 555-010{i}", city="Salt Lake City", raw_categories=cats,
                    yelp_reviews=200)
        score_lead(lead, config.DEFAULT_KEYWORDS)
        leads.append(lead)
    saved.save_search(leads, config.DEFAULT_KEYWORDS)
    context = browser.new_context(viewport={"width": 1280, "height": 900}, accept_downloads=True)
    tab = context.new_page()
    errors = []
    tab.on("pageerror", lambda exc: errors.append(str(exc)))
    tab.on("dialog", lambda d: errors.append(f"native dialog: {d.message}"))
    tab.goto(site + "#leads")
    tab.wait_for_selector("table.leads")
    yield tab
    context.close()
    assert not errors


def _row(page, name):
    return page.locator("table.leads tbody tr", has_text=name)


def test_mark_yes_moves_the_lead_and_undo_brings_it_back(page):
    _row(page, "Costco").locator(".mark button.yes").click()
    page.wait_for_selector("#recent:not([hidden])")
    assert "Costco Wholesale: marked Yes" in page.inner_text("#recent")
    assert _row(page, "Costco").count() == 0                    # left Not checked
    page.get_by_role("button", name="Has baler or compactor (1)").click()
    assert _row(page, "Costco").count() == 1
    assert "Undo (4:" in _row(page, "Costco").inner_text()
    _row(page, "Costco").locator("button.undo").click()
    page.wait_for_selector("text=Not checked (3)")
    assert page.locator("#recent").is_hidden()


def test_just_called_lands_in_the_calls_tab(page):
    _row(page, "Smith").locator(".mark button.yes").click()
    page.get_by_role("button", name="Has baler or compactor (1)").click()
    _row(page, "Smith").get_by_role("button", name="Just called").click()
    page.fill("#call-notes", "Spoke to Dana; send a quote.")
    page.locator("#outcomes").get_by_role("button", name="Follow Up").click()
    page.click("#call-save")
    page.wait_for_selector("#call-dlg:not([open])", state="attached")
    page.click("nav a[data-page=calls]")
    page.get_by_role("button", name="Follow Up (1)").click()
    table = page.inner_text("#calls-wrap")
    assert "Smith's Marketplace" in table and "Spoke to Dana; send a quote." in table
    assert page.get_attribute("#calls-wrap a[href^='tel:']", "href") == "tel:8015550100"


def test_stats_count_the_marks(page):
    _row(page, "Costco").locator(".mark button.yes").click()
    page.wait_for_selector("#recent:not([hidden])")
    _row(page, "Hampton").locator(".mark button.no").click()
    page.wait_for_function("document.getElementById('recent-list').children.length === 2")
    page.click("nav a[data-page=stats]")
    page.wait_for_function("document.getElementById('s-total').textContent === '1'")
    assert page.locator("#chart svg").count() == 1
    rows = page.locator("#s-table tbody tr")
    assert rows.count() == 4


def test_both_downloads(page):
    for label, ext in (("Download Excel", ".xlsx"), ("Download CSV", ".csv")):
        with page.expect_download() as info:
            page.get_by_role("link", name=label).click()
        assert info.value.suggested_filename.endswith(ext)


def test_the_view_survives_a_reload(page):
    page.get_by_role("button", name="All (3)").click()
    page.fill("#filter", "hampton")
    page.locator("th button.sort", has_text="Miles").click()
    page.reload()
    page.wait_for_selector("table.leads")
    assert page.input_value("#filter") == "hampton"
    assert page.locator("#lead-tabs button.on").inner_text().startswith("All")
    assert page.locator("th[aria-sort=ascending]").inner_text().startswith("Miles")
