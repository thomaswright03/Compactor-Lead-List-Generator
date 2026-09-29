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
    # The row stays put (showing its answer) while the pointer is on the table ...
    assert "Moves to “Has baler or compactor”" in _row(page, "Costco").inner_text()
    page.mouse.move(5, 5)
    # ... and leaves Not checked a few seconds after the pointer moves away.
    page.wait_for_function("!document.querySelector('#leads-wrap').innerText.includes('Costco')",
                           timeout=15000)
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


def _posts(page, path):
    sent = []
    page.on("request", lambda r: sent.append(r.url) if r.method == "POST" and r.url.endswith(path) else None)
    return sent


def test_a_double_click_marks_only_the_business_aimed_at(page):
    sent = _posts(page, "/mark")
    first = page.locator("table.leads tbody tr").first
    name = first.locator("strong").first.inner_text()
    first.locator(".mark button.no").dblclick()
    page.wait_for_selector("text=No baler or compactor (1)")
    page.wait_for_timeout(800)
    assert len(sent) == 1
    assert page.locator("text=Not checked (2)").count() == 1
    page.get_by_role("button", name="No baler or compactor (1)").click()
    assert _row(page, name).count() == 1


def test_find_leads_asks_before_using_the_days_search(page, monkeypatch):
    from leadgen.pipeline import RunResult
    monkeypatch.setattr(web.finding, "run", lambda params, progress: RunResult(
        [], (40.76, -111.89), "SLC", [], {"leads kept": 0, "seconds": 3.2}))
    sent = _posts(page, "/search")
    page.click("nav a[data-page=find]")
    page.wait_for_selector("text=Today's is available")
    page.fill("input[name=radius]", "12")
    page.click("#go")
    dialog = page.locator("#confirm-dlg")
    assert dialog.is_visible()
    text = dialog.inner_text()
    assert "This uses today's only search" in text and "12 miles" in text
    page.click("#confirm-back")
    assert not dialog.is_visible() and not sent
    assert "Today's is available" in page.inner_text("#day-note")
    page.click("#go")
    page.keyboard.press("Escape")                     # the keyboard can back out too
    assert not dialog.is_visible() and not sent
    page.click("#go")
    page.click("#confirm-go")
    page.wait_for_selector("#done-card:not([hidden])")
    assert len(sent) == 1
    page.get_by_role("button", name="Details").first.click()
    details = page.inner_text("#history")
    assert "Leads kept" in details and "3 seconds" in details and "leads kept" not in details


def test_typed_call_notes_survive_closing_the_box(page):
    _row(page, "Smith").locator(".mark button.yes").click()
    page.get_by_role("button", name="Has baler or compactor (1)").click()
    _row(page, "Smith").get_by_role("button", name="Just called").click()
    page.fill("#call-notes", "Spoke to Dana; call back Friday.")
    page.keyboard.press("Escape")
    assert page.locator("#call-dlg").is_hidden()
    _row(page, "Smith").get_by_role("button", name="Just called").click()
    assert page.input_value("#call-notes") == "Spoke to Dana; call back Friday."
    page.click("#call-cancel")
    page.reload()                                      # kept in this browser, even after a reload
    page.wait_for_selector("table.leads")
    page.get_by_role("button", name="Has baler or compactor (1)").click()
    _row(page, "Smith").get_by_role("button", name="Just called").click()
    assert page.input_value("#call-notes") == "Spoke to Dana; call back Friday."
    page.locator("#outcomes").get_by_role("button", name="Interested", exact=True).click()
    page.click("#call-save")
    page.wait_for_selector("#call-dlg:not([open])", state="attached")
    _row(page, "Smith").get_by_role("button", name="Just called").click()
    assert page.input_value("#call-notes") == ""       # saved, so the draft is gone


def test_titles_hints_and_empty_pages(page):
    assert page.title() == "Leads · Arco Compactor Lead Finder"
    assert "mark the business Yes" in page.inner_text("#call-hint")
    page.click("nav a[data-page=calls]")
    page.wait_for_function("document.title === 'Calls · Arco Compactor Lead Finder'")
    assert "Log a call with Just called" in page.inner_text("#calls-wrap")
    page.click("nav a[data-page=stats]")
    page.wait_for_function("document.title === 'Stats · Arco Compactor Lead Finder'")
    page.wait_for_selector("#stats-empty:not([hidden])")
    assert "Mark businesses Yes or No on the Leads page" in page.inner_text("#stats-empty")
    page.click("#stats-empty a")
    page.wait_for_function("document.title.startsWith('Leads')")


def test_a_colleagues_mark_arrives_without_reloading_everything(page, monkeypatch):
    from leadgen import marks
    from leadgen import saved as saved_list
    monkeypatch.setattr(web.leads, "SINCE_OVERLAP", 0.0)   # the leads were saved just now
    costco = next(l for l in saved_list.load() if l.name.startswith("Costco"))
    marks.set_mark(costco.uid, "no")                  # a colleague, elsewhere
    with page.expect_response(lambda r: "/leads?since=" in r.url) as info:
        page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    body = info.value.json()
    assert [l["name"] for l in body["leads"]] == ["Costco Wholesale"]
    page.wait_for_selector("text=No baler or compactor (1)")


def test_touch_targets_on_a_phone(browser, site, page):
    context = browser.new_context(viewport={"width": 375, "height": 800}, has_touch=True,
                                  is_mobile=True)
    phone = context.new_page()
    phone.goto(site + "#leads")
    phone.wait_for_selector("table.leads")
    phone.locator(".mark button.yes").first.tap()
    phone.wait_for_selector("button.undo")
    small = phone.evaluate("""() => [...document.querySelectorAll('button, a, summary, select')]
        .filter((e) => e.offsetParent !== null)
        .map((e) => [e.textContent.trim().slice(0, 30), e.getBoundingClientRect()])
        .filter(([, r]) => r.width > 0 && (r.height < 44 || r.width < 44))
        .map(([t, r]) => `${t}: ${Math.round(r.width)}x${Math.round(r.height)}`)""")
    context.close()
    assert small == []
