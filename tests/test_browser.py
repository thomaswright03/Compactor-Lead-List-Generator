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
expect = sync_api.expect


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
                                      ("Hampton Inn", ["yelp:hotels"]),
                                      ("Pro Baler", ["yelp:junkremoval"])]):
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
    expect(_row(page, "Costco")).to_contain_text("Moves to “Has baler or compactor”")
    page.mouse.move(5, 5)
    # ... and leaves Not checked a few seconds after the pointer moves away.
    expect(_row(page, "Costco")).to_have_count(0, timeout=15000)
    page.get_by_role("button", name="Has baler or compactor (1)").click()
    expect(_row(page, "Costco")).to_have_count(1)
    expect(_row(page, "Costco").locator("button.undo")).to_contain_text("Undo (4:")
    _row(page, "Costco").locator("button.undo").click()
    expect(page.get_by_role("button", name="Not checked (3)")).to_be_visible()
    expect(page.locator("#recent")).to_be_hidden()


def test_just_called_lands_in_the_calls_tab(page):
    _row(page, "Smith").locator(".mark button.yes").click()
    page.get_by_role("button", name="Has baler or compactor (1)").click()
    _row(page, "Smith").get_by_role("button", name="Just called").click()
    page.fill("#call-notes", "Spoke to Dana; send a quote.")
    page.locator("#outcomes").get_by_role("button", name="Follow Up").click()
    page.click("#call-save")
    page.wait_for_selector("#call-dlg:not([open])", state="attached")
    page.click("nav a[data-page=calls]")
    # The Calls page opens on every call, newest first: the one just saved is in view.
    expect(page.locator("#calls-wrap")).to_contain_text("Spoke to Dana; send a quote.")
    assert page.locator("#call-tabs button.on").inner_text() == "All calls (1)"
    page.get_by_role("button", name="Follow Up (1)").click()
    table = page.inner_text("#calls-wrap")
    assert "Smith's Marketplace" in table and "Spoke to Dana; send a quote." in table
    assert page.get_attribute("#calls-wrap a[href^='tel:']", "href") == "tel:8015550100"
    assert page.url.endswith("#calls?tab=Follow+Up")
    page.reload()                                      # a tab in the address is kept
    expect(page.locator("#call-tabs button.on")).to_have_text("Follow Up (1)")


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


def test_the_excel_button_shows_it_is_busy_and_starts_one_download(page):
    asked = []
    page.on("request", lambda r: asked.append(r.url) if "/download/" in r.url else None)
    release = []
    page.route("**/download/saved.xlsx", lambda route: release.append(route))   # held: a slow build
    link = page.locator("a[data-download][href$='.xlsx']")
    link.click()
    expect(link).to_have_text("Preparing Excel…")
    link.click(force=True)                            # an impatient second click
    page.wait_for_timeout(300)
    assert len(asked) == 1 and link.get_attribute("aria-disabled") == "true"
    with page.expect_download() as info:
        release[0].continue_()
    assert info.value.suggested_filename == "compactor-leads.xlsx"
    expect(link).to_have_text("Download Excel")


def test_the_view_survives_a_reload(page):
    page.get_by_role("button", name="All (4)").click()
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
    expect(_row(page, name)).to_have_count(1)


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
    expect(page.locator("#calls-wrap")).to_contain_text("Log a call with Just called")
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
    with page.expect_response(lambda r: "/leads?" in r.url and "since=" in r.url) as info:
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


def test_competitors_are_flagged_not_asked(page):
    assert "Pro Baler" not in page.inner_text("#leads-wrap")      # not in Not checked
    assert page.inner_text("#n-leads") == "3 to check"
    page.get_by_role("button", name="Competitors (1)").click()
    row = _row(page, "Pro Baler")
    expect(row).to_contain_text("Competitor: not a prospect")
    assert row.locator(".mark button").count() == 0
    assert "aren't asked Yes / No" in page.inner_text("#competitor-hint")


def test_a_note_never_follows_to_another_page(page):
    _row(page, "Costco").locator(".mark button.yes").click()
    expect(page.locator("#toast")).to_be_visible()
    page.click("nav a[data-page=stats]")
    expect(page.locator("#toast")).to_be_hidden()
    assert "Costco Wholesale: marked Yes" in page.inner_text("#recent")   # the Undo is still there


def test_stats_show_only_the_note_until_something_is_marked(page):
    page.click("nav a[data-page=stats]")
    page.wait_for_selector("#stats-empty:not([hidden])")
    assert page.locator("#stats-data").is_hidden()
    assert "1 competitor and own-company listing is left out" in page.inner_text("#stats-left-out")
    page.click("#stats-empty a")
    _row(page, "Costco").locator(".mark button.yes").click()
    page.wait_for_selector("#recent:not([hidden])")
    page.click("nav a[data-page=stats]")
    expect(page.locator("#stats-data")).to_be_visible()
    assert page.locator("#stats-empty").is_hidden() and page.inner_text("#s-total") == "1"


@pytest.mark.parametrize("width", [320, 375, 390, 768, 1280])
def test_theme_labels_fit_at_every_width(browser, site, page, width):
    touch = width < 1000                               # phones and tablets: 44 px targets
    context = browser.new_context(viewport={"width": width, "height": 800}, has_touch=touch)
    tab = context.new_page()
    tab.goto(site + "#leads")
    tab.wait_for_selector("table.leads")
    clipped = tab.evaluate("""(touch) => {
        const side = document.querySelector('.side').getBoundingClientRect();
        return [...document.querySelectorAll('.theme button')].filter((b) => {
            const r = b.getBoundingClientRect();
            return b.scrollWidth > b.clientWidth || r.right > side.right || r.left < side.left
                   || (touch && r.height < 44);
        }).map((b) => b.textContent); }""", touch)
    wide = tab.evaluate("document.documentElement.scrollWidth > window.innerWidth")
    context.close()
    assert clipped == [] and not wide
