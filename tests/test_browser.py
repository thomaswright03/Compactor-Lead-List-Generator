"""The pages in a real browser: marks, undo, calls, stats and downloads.

Needs Playwright and its Chromium (pip install playwright; playwright install
chromium). Skipped when they are missing. LEADGEN_CHROMIUM can point at a
Chromium binary to use instead of Playwright's own.
"""

import os
import re
import threading

import pytest

from leadgen import config, saved, web
from leadgen.models import Lead
from leadgen.scoring import score_lead

sync_api = pytest.importorskip("playwright.sync_api")
expect = sync_api.expect


def _context(browser, name="Tester", **kw):
    """A browser window whose "Your name" is already set (so a first Yes / No doesn't ask)."""
    context = browser.new_context(**kw)
    if name:
        context.add_init_script(f"try {{ localStorage.setItem('my-name', {name!r}); }} catch (e) {{}}")
    return context


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
                    yelp_reviews=200, address=f"{100 + i} Main St",
                    map_url=f"https://www.google.com/maps/search/?api=1&query=y{i}")
        score_lead(lead, config.DEFAULT_KEYWORDS)
        leads.append(lead)
    saved.save_search(leads, config.DEFAULT_KEYWORDS)
    context = _context(browser, viewport={"width": 1280, "height": 900}, accept_downloads=True)
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
    # The Calls page opens on every called business, latest call first: the one just saved is in view.
    expect(page.locator("#calls-wrap")).to_contain_text("Spoke to Dana; send a quote.")
    assert page.locator("#call-tabs button.on").inner_text() == "All called businesses (1)"
    page.get_by_role("button", name="Follow Up (1)").click()
    table = page.inner_text("#calls-wrap")
    assert "Smith's Marketplace" in table and "Spoke to Dana; send a quote." in table
    assert page.get_attribute("#calls-wrap a[href^='tel:']", "href") == "tel:8015550100"
    assert page.url.endswith("#calls?tab=Follow+Up")
    page.reload()                                      # a tab in the address is kept
    expect(page.locator("#call-tabs button.on")).to_have_text("Follow Up (1)")


def test_a_call_without_notes_keeps_the_earlier_notes_in_view(page):
    _row(page, "Smith").locator(".mark button.yes").click()
    page.get_by_role("button", name="Has baler or compactor (1)").click()
    for notes, outcome in [("Spoke with store manager Jim; 60-yd compactor, lease ends March", "Follow Up"),
                           ("", "Interested")]:
        _row(page, "Smith").get_by_role("button", name="Just called").click()
        page.fill("#call-notes", notes)
        page.locator("#outcomes").get_by_role("button", name=outcome, exact=True).click()
        page.click("#call-save")
        page.wait_for_selector("#call-dlg:not([open])", state="attached")
    page.click("nav a[data-page=calls]")
    expect(page.locator("#calls-wrap")).to_contain_text("Latest call: no notes.")
    cell = page.inner_text("#calls-wrap td.notes")
    assert "From the call on" in cell and "lease ends March" in cell
    # One row per business, and the tab says so.
    assert page.locator("#calls-wrap tbody tr").count() == 1
    assert page.locator("#call-tabs button.on").inner_text() == "All called businesses (1)"
    assert "2 calls" in page.inner_text("#calls-wrap")
    page.reload()                                      # the same after a reload (from the server)
    expect(page.locator("#calls-wrap td.notes")).to_contain_text("lease ends March")


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
    assert re.fullmatch(r"compactor-leads-\d{4}-\d{2}-\d{2}\.xlsx", info.value.suggested_filename)
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
    expect(dialog).to_be_visible()
    text = dialog.inner_text()
    assert "This uses today's only search" in text and "12 miles" in text
    # The place the search will actually run around, and how far it is from Arco's shop.
    assert "Arco Compactor, 876 Fortune Rd" in text and "0.0 miles" in text
    assert page.locator("#confirm-far").is_hidden()
    page.click("#confirm-back")
    assert not dialog.is_visible() and not sent
    assert "Today's is available" in page.inner_text("#day-note")
    page.click("#go")
    expect(dialog).to_be_visible()
    page.keyboard.press("Escape")                     # the keyboard can back out too
    assert not dialog.is_visible() and not sent
    page.click("#go")
    expect(dialog).to_be_visible()
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
    assert "whatever its answer" in page.inner_text("#call-hint")
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
    # It left Not checked, so only its key comes back (the page drops the row).
    assert body["leads"] == [{"key": costco.uid, "in_view": False}]
    page.wait_for_selector("text=No baler or compactor (1)")
    expect(_row(page, "Costco")).to_have_count(0)


def test_touch_targets_on_a_phone(browser, site, page):
    context = _context(browser, viewport={"width": 375, "height": 800}, has_touch=True,
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
    context = _context(browser, viewport={"width": width, "height": 800}, has_touch=touch)
    tab = context.new_page()
    tab.goto(site + "#leads")
    tab.wait_for_selector("table.leads")
    if tab.locator("#menu-btn").is_visible():          # phones: the theme switch is in the menu
        tab.click("#menu-btn")
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


def test_find_leads_says_why_a_search_cannot_start(page):
    page.click("nav a[data-page=find]")
    page.wait_for_selector("text=Today's is available")
    assert "up to 20 words, each up to 60 characters" in page.inner_text("#form")
    # Caught in the browser, before the confirmation opens.
    page.fill("input[name=keywords]", "x" * 70)
    page.click("#go")
    assert not page.locator("#confirm-dlg").is_visible()
    expect(page.locator("#err-keywords")).to_contain_text("up to 60 characters")
    assert page.get_attribute("input[name=keywords]", "aria-invalid") == "true"
    assert "Today's is available" in page.inner_text("#day-note")
    # Editing the form clears it.
    page.fill("input[name=keywords]", "baler")
    expect(page.locator("#err-keywords")).to_have_count(0)
    # An empty "Search around" is never quietly replaced by Arco's address.
    page.fill("input[name=location]", "")
    page.click("#go")
    assert not page.locator("#confirm-dlg").is_visible()
    expect(page.locator("#err-location")).to_contain_text("Enter where to search around")
    page.fill("input[name=location]", "84101")
    # The server's own refusal (the browser check skipped) shows too, and stays.
    page.evaluate("document.querySelector('input[name=keywords]').value = Array(22).fill('w').join(',')")
    page.evaluate("startSearch()")
    expect(page.locator("#err-keywords")).to_contain_text("Use at most 20 search words")
    page.wait_for_timeout(300)                         # the search history has reloaded by now
    expect(page.locator("#err-keywords")).to_contain_text("Use at most 20 search words")
    # Shown once, beside its field: the red line under the button is for other problems.
    expect(page.locator("#go-error")).to_be_hidden()
    expect(page.get_by_text("Use at most 20 search words")).to_have_count(1)
    assert "red" not in page.evaluate("getComputedStyle(document.getElementById('day-note')).color")


def test_a_refused_search_says_so(page):
    from leadgen import daily
    day, _ = daily.claim({"location": "84101"})      # someone else ran today's search
    daily.finish(day, {"leads": 0})
    page.click("nav a[data-page=find]")
    page.wait_for_selector("#day-note:has-text('The next one can run tomorrow')")
    # A normal state, not an error: muted, not red.
    colour = page.evaluate("getComputedStyle(document.getElementById('day-note')).color")
    error = page.evaluate("getComputedStyle(document.getElementById('go-error')).color")
    assert colour != error
    page.evaluate("startSearch()")
    expect(page.locator("#go-error")).to_contain_text("Today's search was already run")


def test_leads_are_cards_on_a_phone(browser, site, page):
    context = _context(browser, viewport={"width": 375, "height": 812}, has_touch=True, is_mobile=True)
    phone = context.new_page()
    phone.goto(site + "#leads")
    phone.wait_for_selector("table.leads")
    fits = phone.evaluate("""() => {
        const wrap = document.getElementById('leads-wrap');
        const inside = (e) => { const r = e.getBoundingClientRect();
                                return r.left >= 0 && r.right <= window.innerWidth; };
        return { wide: wrap.scrollWidth > wrap.clientWidth,
                 page: document.documentElement.scrollWidth > window.innerWidth,
                 rows: [...document.querySelectorAll('table.leads tbody tr')].slice(0, 5).map((tr) =>
                   [...tr.querySelectorAll('td.c-name strong, .mark button')].every(inside)) }; }""")
    assert not fits["wide"] and not fits["page"] and fits["rows"] and all(fits["rows"])
    # The name sits right above its own Yes / No buttons.
    row = phone.locator("table.leads tbody tr").first
    name, yes = row.locator("td.c-name strong").bounding_box(), row.locator(".mark button.yes").bounding_box()
    assert 0 < yes["y"] - (name["y"] + name["height"]) < 120
    # The reasons open with a toggle.
    assert row.locator(".reasons").is_hidden()
    row.get_by_role("button", name="Why this score").tap()
    assert row.locator(".reasons").is_visible()
    # The page title starts near the top: the navigation is one short bar.
    assert phone.locator("#page-leads h1").bounding_box()["y"] < 140
    assert phone.locator(".side").bounding_box()["height"] <= 112
    phone.click("#menu-btn")
    assert phone.locator(".theme").is_visible()
    phone.select_option("#sort-pick", "name:asc")
    phone.wait_for_function("location.hash.includes('sort=name')")
    context.close()


def _uid(name):
    return next(lead.uid for lead in saved.load() if lead.name.startswith(name))


def test_calls_are_cards_and_every_page_is_in_the_bar_on_a_small_phone(browser, site, page):
    from leadgen import calls, marks
    uid = _uid("Smith")
    marks.set_mark(uid, "yes")
    calls.log_call(uid, "Follow Up", "Spoke to Dana; send a quote.")
    context = _context(browser, viewport={"width": 320, "height": 700}, has_touch=True, is_mobile=True)
    phone = context.new_page()
    phone.goto(site + "#calls")
    phone.wait_for_selector("table.calls")
    fits = phone.evaluate("""() => {
        const inside = (e) => { const r = e.getBoundingClientRect();
                                return r.width > 0 && r.left >= 0 && r.right <= window.innerWidth; };
        const wrap = document.getElementById('calls-wrap');
        return { wide: wrap.scrollWidth > wrap.clientWidth,
                 page: document.documentElement.scrollWidth > window.innerWidth,
                 parts: [...document.querySelectorAll('#calls-wrap td.notes, #calls-wrap .call-cell button')]
                        .map(inside),
                 nav: [...document.querySelectorAll('nav a')].map(inside) }; }""")
    assert not fits["wide"] and not fits["page"]
    assert len(fits["parts"]) >= 3 and all(fits["parts"])    # notes, Just called, History, Undo
    assert fits["nav"] == [True] * 4                         # Stats is never cut off
    context.close()


def test_a_business_closed_for_good_keeps_its_mark_in_view(page):
    uid = _uid("Costco")
    from leadgen import marks
    marks.set_mark(uid, "yes")
    closed = next(lead for lead in saved.load() if lead.uid == uid)
    again = Lead(name=closed.name, lat=closed.lat, lon=closed.lon, source="yelp", source_id="y1",
                 raw_categories=["yelp:wholesale_stores"], business_status="CLOSED_PERMANENTLY")
    score_lead(again, config.DEFAULT_KEYWORDS)
    saved.save_search([again], config.DEFAULT_KEYWORDS)
    page.reload()
    page.wait_for_selector("table.leads")
    expect(page.get_by_role("button", name="Not checked (2)")).to_be_visible()
    page.get_by_role("button", name="Closed (1)").click()
    expect(_row(page, "Costco")).to_contain_text("Closed for good")
    expect(page.locator("#closed-hint")).to_be_visible()
    page.get_by_role("button", name="Has baler or compactor (1)").click()
    expect(_row(page, "Costco").locator(".mark button.yes.on")).to_have_count(1)


def test_skip_link_and_short_recent_changes(page):
    for name in ("Costco", "Smith", "Hampton"):
        _row(page, name).locator(".mark button.yes").click()
        page.wait_for_timeout(800)                      # past the guard against double clicks
    page.wait_for_function("document.getElementById('recent-list').children.length === 2")
    expect(page.locator("#recent-more")).to_have_text("Show all 3")
    page.click("#recent-more")
    page.wait_for_function("document.getElementById('recent-list').children.length === 3")
    page.reload()
    page.wait_for_selector("#lead-tabs button")        # every business is checked: no table now
    page.keyboard.press("Tab")
    assert page.evaluate("document.activeElement.id") == "skip"
    page.keyboard.press("Enter")
    assert page.evaluate("document.activeElement.id") == "leads-wrap"


def test_a_call_can_be_logged_before_the_business_is_marked(page):
    _row(page, "Smith").get_by_role("button", name="Just called").click()
    page.fill("#call-notes", "No answer; try the morning.")
    page.locator("#outcomes").get_by_role("button", name="No Contact").click()
    page.click("#call-save")
    page.wait_for_selector("#call-dlg:not([open])", state="attached")
    expect(_row(page, "Smith")).to_contain_text("No Contact")
    expect(page.get_by_role("button", name="Not checked (3)")).to_be_visible()   # still to check
    page.click("nav a[data-page=calls]")
    page.get_by_role("button", name="No Contact (1)").click()
    expect(page.locator("#calls-wrap")).to_contain_text("No answer; try the morning.")


def test_a_double_click_on_start_search_selects_nothing(page, monkeypatch):
    from leadgen.pipeline import RunResult
    monkeypatch.setattr(web.finding, "run", lambda params, progress: RunResult(
        [], (40.76, -111.89), "SLC", [], {"leads kept": 0, "seconds": 3.2}))
    sent = _posts(page, "/search")
    page.click("nav a[data-page=find]")
    page.wait_for_selector("text=Today's is available")
    page.click("#go")
    page.dblclick("#confirm-go")
    page.wait_for_selector("#done-card:not([hidden])")
    assert len(sent) == 1
    assert page.evaluate("window.getSelection().toString()") == ""


def test_missing_address_and_tier_meaning_are_written_out(browser, site, page):
    lead = Lead(name="Nowhere Foods", lat=40.7, lon=-111.9, source="yelp", source_id="y9",
                phone="(801) 555-0199", city="", raw_categories=["yelp:grocery"], yelp_reviews=200,
                map_url="https://www.google.com/maps/search/?api=1&query=y9")
    score_lead(lead, config.DEFAULT_KEYWORDS)
    saved.save_search([lead], config.DEFAULT_KEYWORDS)
    page.reload()
    row = _row(page, "Nowhere Foods")
    expect(row.locator("td.contact")).to_contain_text("No street address")
    expect(row.locator("td.contact a.map")).to_have_count(1)
    words = {"A": "strong", "B": "likely", "C": "possible", "D": "weak"}
    score = row.locator("td.score")
    tier = score.inner_text().split()[1]
    assert score.inner_text().split()[2] == words[tier] and words[tier] in score.get_attribute("title")
    phone = _context(browser, viewport={"width": 390, "height": 844}, has_touch=True)
    tab = phone.new_page()
    tab.goto(site + "#leads")
    card = tab.locator("table.leads tbody tr", has_text="Nowhere Foods")
    expect(card).to_contain_text("No street address")
    expect(card.locator("td.score")).to_contain_text(f"{tier} {words[tier]}")
    assert tab.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    phone.close()


# ---- round 7: every width shows the whole lead; the first lead in view on a phone

@pytest.mark.parametrize("width,height", [(768, 1024), (1024, 800), (1100, 800), (1280, 800),
                                          (1440, 900)])
def test_the_whole_lead_and_its_reasons_fit_tablets_and_laptops(browser, site, page, width, height):
    context = _context(browser, viewport={"width": width, "height": height}, has_touch=width < 1000)
    tab = context.new_page()
    tab.goto(site + "#leads")
    tab.wait_for_selector("table.leads")
    fits = tab.evaluate("""() => {
        const wrap = document.getElementById('leads-wrap');
        const reasons = [...document.querySelectorAll('table.leads .reasons')];
        return { wide: wrap.scrollWidth > wrap.clientWidth,
                 page: document.documentElement.scrollWidth > window.innerWidth,
                 shown: reasons.length > 0 && reasons.every((r) => r.offsetParent !== null
                        && r.getBoundingClientRect().right <= wrap.getBoundingClientRect().right + 1) }; }""")
    context.close()
    assert not fits["wide"] and not fits["page"] and fits["shown"]


def test_the_first_lead_is_in_view_on_a_phone(browser, site, page):
    from leadgen import marks
    marks.set_mark(_uid("Smith"), "yes", "Dana")          # Recent changes has a line
    context = _context(browser, viewport={"width": 375, "height": 812}, has_touch=True, is_mobile=True)
    phone = context.new_page()
    phone.goto(site + "#leads")
    phone.wait_for_selector("table.leads")
    phone.wait_for_selector("#recent:not([hidden])")
    assert phone.evaluate("document.getElementById('leads-wrap').getBoundingClientRect().top") < 812
    card = phone.locator("table.leads tbody tr").first
    for part in (card.locator("td.c-name strong"), card.locator(".mark button.yes"),
                 card.locator(".mark button.no")):
        box = part.bounding_box()
        assert box and box["y"] + box["height"] <= 812
    # The tabs are one list there, and the downloads come after the list.
    assert phone.locator("#lead-tabs").is_hidden()
    phone.select_option("#tab-pick", "yes")
    expect(_row(phone, "Smith")).to_have_count(1)
    downloads = phone.locator("#downloads").bounding_box()
    assert downloads["y"] > phone.locator("#leads-wrap").bounding_box()["y"]
    assert "by Dana" in phone.inner_text("#recent")
    context.close()


def test_your_name_is_asked_once_and_shown_with_the_mark(browser, site, page):
    context = _context(browser, name=None, viewport={"width": 1280, "height": 900})
    tab = context.new_page()
    tab.goto(site + "#leads")
    tab.wait_for_selector("table.leads")
    sent = _posts(tab, "/mark")
    _row(tab, "Costco").locator(".mark button.yes").click()
    expect(tab.locator("#name-dlg")).to_be_visible()
    assert sent == []                                  # nothing saved before the name is known
    tab.fill("#name-input", "Dana")
    tab.click("#name-save")
    tab.wait_for_selector("#recent:not([hidden])")
    assert "Costco Wholesale: marked Yes by Dana" in tab.inner_text("#recent")
    assert tab.inner_text("#me-name") == "Dana"
    # Asked once: the next click goes straight through.
    _row(tab, "Smith").locator(".mark button.no").click()
    tab.wait_for_function("document.getElementById('recent').innerText.includes('Smith')")
    assert not tab.locator("#name-dlg").is_visible()
    tab.get_by_role("button", name="All (4)").click()
    expect(_row(tab, "Costco")).to_contain_text("Marked by Dana")
    context.close()


def test_an_empty_list_points_to_find_leads(browser, site):
    context = _context(browser, viewport={"width": 1280, "height": 900})
    tab = context.new_page()
    tab.goto(site + "#leads")
    tab.wait_for_selector("#leads-wrap .empty")
    assert tab.locator("#downloads").is_hidden()
    tab.get_by_role("link", name="Go to Find leads").click()
    expect(tab.locator("#page-find")).to_be_visible()
    context.close()


def test_no_match_names_the_filter_and_clears_it(page):
    page.fill("#filter", "walmrt")
    expect(page.locator("#leads-wrap .empty")).to_contain_text("No leads match “walmrt” in Not checked")
    page.get_by_role("button", name="Clear filters").click()
    expect(page.locator("table.leads tbody tr")).to_have_count(3)
    assert page.input_value("#filter") == ""


@pytest.mark.parametrize("width", [1280, 1440])
def test_a_four_digit_lead_count_stays_on_one_line(browser, site, page, width):
    context = _context(browser, viewport={"width": width, "height": 900})
    tab = context.new_page()
    tab.goto(site + "#leads")
    tab.wait_for_selector("#n-leads:not([hidden])")
    tab.evaluate("S.counts.unchecked = 1135; counts()")
    link = tab.locator("nav a[data-page=leads]").bounding_box()
    badge = tab.locator("#n-leads").bounding_box()
    context.close()
    assert tab is not None and link["height"] < 48 and badge["y"] < link["y"] + link["height"] / 2


def test_reading_text_is_at_least_14px(page):
    sizes = page.evaluate("""() => ['.reason', '.contact', 'td.c-name .sub', '.hint-line', 'footer.copy']
        .map((s) => document.querySelector(s)).filter(Boolean)
        .map((e) => parseFloat(getComputedStyle(e).fontSize))""")
    assert len(sizes) >= 4 and min(sizes) >= 14


# ---- one site, admin controls out of the way, a name on every mark

def test_the_name_cannot_be_skipped(browser, site, page):
    context = _context(browser, name=None, viewport={"width": 1280, "height": 900})
    tab = context.new_page()
    tab.goto(site + "#leads")
    tab.wait_for_selector("table.leads")
    sent = _posts(tab, "/mark")
    _row(tab, "Costco").locator(".mark button.yes").click()
    expect(tab.locator("#name-dlg")).to_be_visible()
    assert tab.locator("#name-dlg").get_by_role("button", name="Not now").count() == 0
    tab.click("#name-save")                             # empty: not saved, asked again
    expect(tab.locator("#name-error")).to_be_visible()
    assert sent == []
    tab.click("#name-cancel")                           # Cancel drops the click
    expect(tab.locator("#name-dlg")).to_be_hidden()
    assert sent == []
    _row(tab, "Costco").locator(".mark button.yes").click()
    expect(tab.locator("#name-dlg")).to_be_visible()     # still asked: no name yet
    tab.fill("#name-input", "Dana")
    tab.click("#name-save")
    tab.wait_for_selector("#recent:not([hidden])")
    assert "Costco Wholesale: marked Yes by Dana" in tab.inner_text("#recent")
    context.close()


def test_admin_controls_are_closed_and_switches_ask_first(page):
    page.click("nav a[data-page=find]")
    page.wait_for_selector("text=Today's is available")
    assert not page.locator("#switch-list").is_visible()
    assert not page.locator("#problems").is_visible()
    page.click("#admin-card summary")
    row = page.locator(".switch-row", has_text="Searching:")
    expect(row).to_contain_text("Searching: Working normally")
    sent = _posts(page, "/switches")
    row.get_by_role("button", name="Pause searching").click()
    dialog = page.locator("#switch-dlg")
    expect(dialog).to_contain_text("Pause all searching for everyone?")
    expect(dialog.locator("#switch-go")).to_have_text("Pause searching")
    page.click("#switch-cancel")
    expect(dialog).to_be_hidden()
    assert sent == []
    expect(row).to_contain_text("Working normally")
    row.get_by_role("button", name="Pause searching").click()
    page.click("#switch-go")
    expect(row).to_contain_text("Searching: Paused")
    expect(row.get_by_role("button", name="Resume searching")).to_be_visible()
    assert len(sent) == 1
    expect(page.locator("#paused-box")).to_be_visible()


def test_extra_search_words_start_empty_and_the_confirmation_says_so(page):
    page.click("nav a[data-page=find]")
    page.wait_for_selector("text=Today's is available")
    assert page.input_value("input[name=keywords]") == ""
    assert "are always searched" in page.inner_text("#form")
    page.click("#go")
    expect(page.locator("#confirm-dlg")).to_contain_text("Nothing extra")
    page.click("#confirm-back")
    page.fill("input[name=keywords]", "pallets")
    page.click("#go")
    expect(page.locator("#confirm-list")).to_contain_text("pallets")
    page.click("#confirm-back")


def test_no_competitors_names_them_and_has_phone_filters(browser, site):
    leads = []
    for i, (name, phone) in enumerate([("Harmons Grocery", "(801) 555-0199"), ("Maceys", ""),
                                       ("Smiths Marketplace", "(801) 555-0100")]):
        lead = Lead(name=name, lat=40.72 + i * 0.01, lon=-111.9, source="yelp", source_id=f"p{i}",
                    phone=phone, raw_categories=["yelp:grocery"], yelp_reviews=200)
        score_lead(lead, config.DEFAULT_KEYWORDS)
        leads.append(lead)
    saved.save_search(leads, config.DEFAULT_KEYWORDS)
    context = _context(browser, viewport={"width": 1280, "height": 900})
    tab = context.new_page()
    tab.goto(site + "#leads")
    tab.wait_for_selector("table.leads")
    rows = tab.locator("table.leads tbody tr")
    expect(rows).to_have_count(3)
    assert "Maceys" in rows.nth(2).inner_text()        # same score: the ones with a phone first
    tab.check("#has-phone")
    expect(rows).to_have_count(2)
    assert "Maceys" not in tab.inner_text("#leads-wrap")
    assert "phone=1" in tab.url
    tab.uncheck("#has-phone")
    tab.get_by_role("button", name="Competitors (0)").click()
    expect(tab.locator("#leads-wrap .empty")).to_contain_text(
        "No Pro Baler, Action Compaction or Arco Compactor listings found in the saved leads yet.")
    context.close()


@pytest.mark.parametrize("width", [1100, 1440, 2560])
def test_log_out_stays_on_one_line(browser, width):
    from werkzeug.serving import make_server
    server = make_server("127.0.0.1", 0, web.create_app(password="pw", username="Maximilian Johannsen"),
                         threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        context = _context(browser, viewport={"width": width, "height": 900})
        tab = context.new_page()
        url = f"http://127.0.0.1:{server.server_port}/"
        tab.goto(url + "login")
        tab.fill("input[name=username]", "Maximilian Johannsen")
        tab.fill("input[name=password]", "pw")
        tab.locator("form button[type=submit]").click()
        tab.wait_for_selector(".side .who button")
        button = tab.locator(".side .who button")
        box = button.bounding_box()
        line = tab.evaluate("parseFloat(getComputedStyle(document.querySelector('.side .who button')).lineHeight)")
        assert box["height"] < 2 * line, "Log out wrapped onto two lines"
        context.close()
    finally:
        server.shutdown()


LONG_WORD = "https://example.com/" + "x" * 80         # a pasted address with no place to break


@pytest.mark.parametrize("width", [375, 1440])
def test_long_unbroken_notes_wrap_and_nothing_scrolls_sideways(browser, site, page, width):
    from leadgen import calls, marks
    uid = _uid("Smith")
    marks.set_mark(uid, "yes", by="Dana")
    calls.log_call(uid, "Follow Up", f"Send the quote to {LONG_WORD} today.", by="Dana")
    context = _context(browser, viewport={"width": width, "height": 800})
    tab = context.new_page()
    check = """() => {
        const wide = document.documentElement.scrollWidth > window.innerWidth;
        const cut = [...document.querySelectorAll('#calls-wrap td.notes, #hist-list .item')]
            .filter((e) => e.getClientRects().length)
            .some((e) => e.scrollWidth > e.clientWidth + 1 || e.getBoundingClientRect().right > window.innerWidth);
        return { wide, cut }; }"""
    tab.goto(site + "#calls")
    tab.wait_for_selector("#calls-wrap td.notes")
    expect(tab.locator("#calls-wrap")).to_contain_text("x" * 40)
    assert tab.evaluate(check) == {"wide": False, "cut": False}
    tab.locator("#calls-wrap").get_by_role("button", name="History").first.click()
    expect(tab.locator("#hist-list")).to_contain_text("x" * 40)
    assert tab.evaluate(check) == {"wide": False, "cut": False}
    context.close()


def test_search_history_is_stacked_cards_on_a_phone(browser, site, page):
    from leadgen import daily
    day, _ = daily.claim({"location": "876 Fortune Rd, Salt Lake City, UT 84104", "radius": 30})
    daily.release(day, "The map data service answered for only part of the area (about 8 of 9 areas "
                       "searched), so some of its businesses are missing from this search.",
                  {"partial": True, "leads": 1038, "new": 1038})
    context = _context(browser, viewport={"width": 375, "height": 800})
    tab = context.new_page()
    tab.goto(site + "#find")
    tab.wait_for_selector("#history table.hist")
    card = tab.locator("#history tbody").first
    text = card.inner_text()
    for part in ("Fortune Rd", "Radius", "30 mi", "Leads", "1,038", "Incomplete", "New",
                 "about 8 of 9 areas", "didn't use up the day's search"):
        assert part in text, part
    fits = tab.evaluate("""() => {
        const out = (e) => { const r = e.getBoundingClientRect();
                             return r.width > 0 && (r.left < 0 || r.right > window.innerWidth); };
        return { page: document.documentElement.scrollWidth > window.innerWidth,
                 out: [...document.querySelectorAll('#history td')].some(out),
                 head: getComputedStyle(document.querySelector('#history thead')).display }; }""")
    assert fits == {"page": False, "out": False, "head": "none"}
    context.close()


def test_the_confirmation_names_the_sources_and_stats_explain_a_missing_average(page):
    page.click("nav a[data-page=find]")
    page.wait_for_selector("text=Today's is available")
    page.click("#go")
    expect(page.locator("#confirm-list")).to_contain_text(
        "Free map data only (Google and Yelp aren't set up)")
    assert "Everywhere available" not in page.inner_text("#confirm-list")
    page.click("#confirm-back")
    page.click("nav a[data-page=leads]")
    _row(page, "Costco").locator(".mark button.no").click()
    page.wait_for_selector("#recent:not([hidden])")
    page.click("nav a[data-page=stats]")
    expect(page.locator("#s-avg")).to_have_text("No businesses marked Yes yet.")


def test_save_without_a_result_says_what_to_pick(page):
    _row(page, "Smith").get_by_role("button", name="Just called").click()
    page.fill("#call-notes", "Spoke to Dana.")
    expect(page.locator("#call-need")).to_have_text("Pick one to save the call.")
    assert page.locator("#call-save").is_enabled()
    page.click("#call-save")
    expect(page.locator("#call-error")).to_contain_text("Pick how the call went")
    assert page.locator("#call-dlg").is_visible()
    assert "need" in page.get_attribute("#outcomes", "class")
    page.locator("#outcomes").get_by_role("button", name="Follow Up").click()
    expect(page.locator("#call-error")).to_have_text("")
    expect(page.locator("#call-need")).to_be_hidden()
    page.click("#call-save")
    page.wait_for_selector("#call-dlg:not([open])", state="attached")


def test_the_calls_page_can_be_filtered_and_keeps_the_filter(page):
    for name in ("Smith", "Costco"):
        _row(page, name).get_by_role("button", name="Just called").click()
        page.locator("#outcomes").get_by_role("button", name="Follow Up").click()
        page.click("#call-save")
        page.wait_for_selector("#call-dlg:not([open])", state="attached")
    page.click("nav a[data-page=calls]")
    expect(page.locator("#calls-wrap tbody tr")).to_have_count(2)
    page.fill("#call-filter", "costco")
    expect(page.locator("#calls-wrap tbody tr")).to_have_count(1)
    assert "Costco Wholesale" in page.inner_text("#calls-wrap")
    assert page.locator("#call-tabs button.on").inner_text() == "All called businesses (1)"
    assert "q=costco" in page.url
    page.reload()
    expect(page.locator("#calls-wrap tbody tr")).to_have_count(1)
    assert page.input_value("#call-filter") == "costco"
    page.get_by_role("button", name="Follow Up (1)").click()
    assert "tab=Follow+Up" in page.url and "q=costco" in page.url
    page.fill("#call-filter", "walmrt")
    expect(page.locator("#calls-wrap")).to_contain_text("No calls match “walmrt”")
    page.get_by_role("button", name="Clear filter").click()
    expect(page.locator("#calls-wrap tbody tr")).to_have_count(2)


def test_several_words_filter_the_leads_and_the_calls_alike(page):
    """A name plus a town, in either order, finds the business on both pages."""
    for query in ("costco salt lake", "Lake COSTCO"):
        page.fill("#filter", query)
        expect(page.locator("table.leads tbody tr")).to_have_count(1)
        assert "Costco Wholesale" in page.inner_text("table.leads tbody")
    page.fill("#filter", "costco provo")
    expect(page.locator("#leads-wrap")).to_contain_text("No leads match “costco provo”")
    page.fill("#filter", "")
    expect(page.locator("table.leads tbody tr")).to_have_count(3)      # Pro Baler is a competitor
    for name in ("Smith", "Costco"):
        _row(page, name).get_by_role("button", name="Just called").click()
        page.locator("#outcomes").get_by_role("button", name="Follow Up").click()
        page.click("#call-save")
        page.wait_for_selector("#call-dlg:not([open])", state="attached")
    page.click("nav a[data-page=calls]")
    for query in ("costco salt lake", "Lake COSTCO"):
        page.fill("#call-filter", query)
        expect(page.locator("#calls-wrap tbody tr")).to_have_count(1)
        assert "Costco Wholesale" in page.inner_text("#calls-wrap")
    page.fill("#call-filter", "costco provo")
    expect(page.locator("#calls-wrap")).to_contain_text("No calls match “costco provo”")


def test_the_calls_filter_finds_what_was_said_how_it_went_and_who_called(page):
    from leadgen import calls
    leads = {l.name: l for l in saved.load()}
    calls.log_call(leads["Costco Wholesale"].uid, "Follow Up", "They load the dumpster with a forklift",
                   by="Dana Smith")
    calls.log_call(leads["Hampton Inn"].uid, "Interested", "Wants a quote", by="Lee")
    page.click("nav a[data-page=calls]")
    rows = page.locator("#calls-wrap tbody tr")
    expect(rows).to_have_count(2)
    assert "call notes or caller" in page.get_attribute("#call-filter", "placeholder")
    for query, name in (("forklift", "Costco Wholesale"), ("dana", "Costco Wholesale"),
                        ("LEE", "Hampton Inn"), ("interested", "Hampton Inn"), ("quote lee", "Hampton Inn")):
        page.fill("#call-filter", query)
        expect(rows).to_have_count(1)
        expect(page.locator("#calls-wrap tbody")).to_contain_text(name)
    expect(page.locator("#call-tabs button.on")).to_have_text("All called businesses (1)")
    page.fill("#call-filter", "pallets")
    expect(page.locator("#calls-wrap")).to_contain_text("No calls match “pallets”")
    expect(page.locator("#calls-wrap")).to_contain_text("in what was said on its calls")


def _called(n):
    """n more saved businesses, each called once, the first one earliest."""
    from leadgen import calls, store
    leads = [Lead(name=f"Called Business {i}", lat=40.6 + i * 0.001, lon=-111.95, source="osm", source_id=f"cb{i}",
                  raw_categories=["shop=wholesale"]) for i in range(n)]
    for lead in leads:
        score_lead(lead, config.DEFAULT_KEYWORDS)
    saved.save_search(leads, config.DEFAULT_KEYWORDS)
    with store.connect() as db:
        db.many("INSERT INTO calls (id, uid, at, outcome, notes) VALUES (?, ?, ?, ?, ?)",
                [(f"{i:032x}", lead.uid, 1_700_000_000.0 + i * 60, calls.OUTCOMES[i % 2], f"Call {i}")
                 for i, lead in enumerate(leads)])
    return leads


def test_the_calls_page_reaches_every_called_business(page):
    """One page of called businesses at a time, "Show more" adding the next, the counts
    always counting every one: the first business ever called is reachable."""
    _called(130)
    page.click("nav a[data-page=calls]")
    rows = page.locator("#calls-wrap tbody tr")
    expect(rows).to_have_count(100)
    expect(page.locator("#call-tabs button.on")).to_have_text("All called businesses (130)")
    expect(page.locator("#call-tabs")).to_contain_text("Interested (65)")
    expect(page.locator("#call-tabs")).to_contain_text("Follow Up (65)")
    assert "Called Business 129" in rows.first.inner_text()           # the latest call first
    more = page.locator("#calls-more")
    expect(more).to_have_text("Show more (30 left)")
    more.click()
    expect(rows).to_have_count(130)
    assert "Called Business 0" in rows.last.inner_text()              # the first call ever
    expect(more).to_be_hidden()
    # An outcome's tab pages the same way, with its own count.
    page.get_by_role("button", name="Follow Up (65)").click()
    expect(rows).to_have_count(65)
    expect(more).to_be_hidden()
    assert "Called Business 1" in rows.last.inner_text()


def _hold(page, part):
    """Hold back the page's /leads requests whose address has `part` (a slow connection);
    returns them, to let them through with .continue_()."""
    held = []

    def handle(route):
        if part in route.request.url:
            held.append(route)
        else:
            route.continue_()
    page.route("**/leads?*", handle)
    return held


def test_a_slow_view_change_says_it_is_loading(page):
    """A tab, filter or sort taking a while: "Loading…" over the dimmed rows within a
    second, a plain "taking longer than usual" after about five, gone once the rows come."""
    held = _hold(page, "tab=all")
    page.locator("#lead-tabs button", has_text="All").click()
    cue = page.locator("#leads-loading")
    expect(cue).to_contain_text("Loading…", timeout=1000)
    expect(cue).to_be_visible()
    assert page.get_attribute("#leads-wrap", "aria-busy") == "true"
    assert cue.get_attribute("role") == "status"
    expect(cue).to_contain_text("This is taking longer than usual", timeout=6000)
    for route in held:
        route.continue_()
    page.unroute("**/leads?*")
    expect(page.locator("table.leads tbody tr")).to_have_count(4)
    expect(cue).to_be_empty()
    assert page.get_attribute("#leads-wrap", "aria-busy") == "false"
    # The Calls page's filter says so too.
    from leadgen import calls
    calls.log_call(saved.load()[0].uid, "Follow Up", "Call back Monday", by="Dana")
    page.click("nav a[data-page=calls]")
    expect(page.locator("#calls-wrap tbody tr")).to_have_count(1)
    held = _hold(page, "q=monday")
    page.fill("#call-filter", "monday")
    cue = page.locator("#calls-loading")
    expect(cue).to_contain_text("Loading…", timeout=1500)
    for route in held:
        route.continue_()
    page.unroute("**/leads?*")
    expect(cue).to_be_empty()
    expect(page.locator("#calls-wrap tbody tr")).to_have_count(1)


@pytest.mark.parametrize("width", [1024, 1440, 2560])
def test_a_calls_time_and_undo_stay_on_one_line_on_a_desktop(browser, site, page, width):
    from leadgen import calls
    for i, lead in enumerate(saved.load()[:3]):
        calls.log_call(lead.uid, "Follow Up", "Spoke to the store manager about the cardboard they bale "
                       "each week and the hauler's monthly bill; call back after the holidays. " * (i + 1),
                       by="Dana Smith")
    context = _context(browser, viewport={"width": width, "height": 900})
    tab = context.new_page()
    tab.goto(site + "#calls")
    tab.wait_for_selector("table.plain.calls button.undo")
    lines = tab.evaluate("""(sel) => [...document.querySelectorAll(sel)].map((e) => {
        const r = document.createRange(); r.selectNodeContents(e);
        return new Set([...r.getClientRects()].map((x) => Math.round(x.bottom))).size; })""",
                         "table.plain.calls .when-at, table.plain.calls button.undo")
    assert len(lines) == 6 and set(lines) == {1}, lines
    assert tab.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    context.close()


def test_a_blank_radius_is_pointed_out(page):
    page.click("nav a[data-page=find]")
    page.wait_for_selector("text=Today's is available")
    page.fill("input[name=radius]", "")
    page.click("#go")
    assert not page.locator("#confirm-dlg").is_visible()
    expect(page.locator("#err-radius")).to_have_text("How far must be between 1 and 100 miles.")


def test_the_form_reads_closed_once_the_day_is_used(page):
    from leadgen import daily
    day, _ = daily.claim({"location": "84101"})
    daily.finish(day, {"leads": 3})
    page.click("nav a[data-page=find]")
    expect(page.locator("#form-closed")).to_be_visible()
    expect(page.locator("#form-closed")).to_contain_text("from midnight Utah time")
    assert page.locator("input[name=location]").is_disabled()
    assert page.locator("input[name=radius]").is_disabled()
    assert page.locator("#go").is_disabled()


def test_miles_have_one_decimal_and_a_category_is_not_repeated(page):
    miles = page.locator("table.leads td.c-miles").all_inner_texts()
    assert miles and all(re.fullmatch(r"\d+\.\d( mi)?", m.strip()) for m in miles), miles
    # A recycling centre's type repeats its category in other words: shown once.
    assert page.evaluate("saysTheSame('Waste / recycling facility', 'Recycling / waste facility')")
    assert not page.evaluate(
        "saysTheSame('Industry (equipment / hauler)', 'Compactor / baler equipment or service')")


def test_address_lines_stay_close_on_a_tablet(browser, site, page):
    context = _context(browser, viewport={"width": 768, "height": 1000}, has_touch=True,
                       is_mobile=True)
    tab = context.new_page()
    tab.goto(site + "#leads")
    tab.wait_for_selector("table.leads td.contact")
    # The phone number's line is one line of text high, while its link keeps a 44px tap area.
    line, tap = tab.evaluate("""() => {
        const a = document.querySelector('table.leads td.contact a[href^="tel:"]');
        return [a.parentElement.getBoundingClientRect().height, a.getBoundingClientRect().height]; }""")
    context.close()
    assert tap >= 44
    assert line < 30, line


@pytest.mark.parametrize("width,height", [(1280, 900), (390, 844)])
def test_the_tutorial_walks_every_page_and_stays_on_screen(browser, site, page, width, height):
    context = _context(browser, viewport={"width": width, "height": height})
    tab = context.new_page()
    errors = []
    tab.on("pageerror", lambda exc: errors.append(str(exc)))
    tab.goto(site + "#leads")
    tab.wait_for_selector("table.leads")
    if width < 700:
        tab.click("#menu-btn")                     # on phones the button is in Menu
    tab.click("#tour-btn")
    box = tab.locator(".tour-box")
    expect(box).to_be_visible()
    expect(tab.locator("#tour-count")).to_have_text(re.compile(r"Step 1 of \d+"))
    total = int(re.search(r"of (\d+)", tab.inner_text("#tour-count")).group(1))
    pages = set()
    for i in range(total):
        expect(tab.locator("#tour-count")).to_have_text(f"Step {i + 1} of {total}")
        tab.wait_for_timeout(250)
        b = box.bounding_box()
        assert b["x"] >= 0 and b["y"] >= 0
        assert b["x"] + b["width"] <= width and b["y"] + b["height"] <= height, tab.inner_text("#tour-title")
        pages.add(tab.evaluate("location.hash.slice(1).split('?')[0]"))
        tab.keyboard.press("Enter")                # Next has the focus
    expect(box).to_be_hidden()
    assert pages == {"find", "leads", "calls", "stats"}
    # Nothing was marked, called or searched along the way.
    tab.goto(site + "#leads")
    tab.wait_for_selector("table.leads")
    assert "Marked by" not in tab.inner_text("table.leads")
    tab.click("#menu-btn") if width < 700 else None
    tab.click("#tour-btn")
    expect(box).to_be_visible()
    tab.keyboard.press("Escape")
    expect(box).to_be_hidden()
    context.close()
    assert not errors


def test_the_administrators_section_unlocks_with_its_password(browser):
    from werkzeug.serving import make_server
    server = make_server("127.0.0.1", 0, web.create_app(password="pw", username="Matt", admin_password="adm1n"),
                         threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        context = _context(browser, viewport={"width": 1280, "height": 900})
        tab = context.new_page()
        url = f"http://127.0.0.1:{server.server_port}/"
        tab.goto(url + "login")
        tab.fill("input[name=username]", "Matt")
        tab.fill("input[name=password]", "pw")
        tab.locator("form button[type=submit]").click()
        tab.goto(url + "#find")
        tab.locator("#admin-card summary").click()
        expect(tab.locator("#admin-content")).to_be_hidden()
        tab.fill("#admin-pass", "wrong")
        tab.click("#admin-unlock")
        expect(tab.locator("#admin-error")).to_have_text("Wrong password.")
        assert tab.url.startswith(url) and "/login" not in tab.url        # still on the page
        tab.fill("#admin-pass", "adm1n")
        tab.keyboard.press("Enter")
        expect(tab.locator("#admin-content")).to_be_visible()
        expect(tab.locator("#switch-list button").first).to_be_visible()
        tab.reload()
        tab.locator("#admin-card summary").click()
        expect(tab.locator("#admin-content")).to_be_visible()             # stays unlocked
        tab.click("#admin-relock")
        expect(tab.locator("#admin-lock")).to_be_visible()
        expect(tab.locator("#admin-content")).to_be_hidden()
        context.close()
    finally:
        server.shutdown()


def test_the_administrators_section_says_when_it_cannot_read_the_saved_data(browser, monkeypatch):
    from werkzeug.serving import make_server

    from leadgen import alerts, daily, switches
    server = make_server("127.0.0.1", 0, web.create_app(password="pw", username="Matt", admin_password="adm1n"),
                         threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        context = _context(browser, viewport={"width": 1280, "height": 900})
        tab = context.new_page()
        url = f"http://127.0.0.1:{server.server_port}/"
        tab.goto(url + "login")
        tab.fill("input[name=username]", "Matt")
        tab.fill("input[name=password]", "pw")
        tab.locator("form button[type=submit]").click()
        tab.goto(url + "#find")
        tab.locator("#admin-card summary").click()
        tab.fill("#admin-pass", "adm1n")
        tab.keyboard.press("Enter")
        expect(tab.locator("#problems")).to_have_text("Nothing went wrong in the last 7 days.")

        def down(*args, **kw):
            raise ConnectionError("the database isn't answering")
        # The problems and the switches can't be read, the history can.
        monkeypatch.setattr(alerts, "recent", down)
        monkeypatch.setattr(switches, "_read", lambda name: (False, "", None, False))
        tab.reload()
        tab.locator("#admin-card summary").click()
        expect(tab.locator("#problems")).to_contain_text("Couldn't load recent problems")
        expect(tab.locator("#switch-list")).to_contain_text("LEADGEN_SEARCH_PAUSED to 1")
        assert "Nothing went wrong" not in tab.locator("#admin-content").inner_text()

        # Nothing can be read: the history's own request fails too.
        monkeypatch.setattr(daily, "history", down)
        tab.locator("#problems button", has_text="Retry").click()
        expect(tab.locator("#history")).to_contain_text("Can't load the search history")
        expect(tab.locator("#problems")).to_contain_text("Couldn't load recent problems")
        expect(tab.locator("#switch-list")).to_contain_text("Couldn't load the site switches")

        monkeypatch.undo()
        tab.locator("#switch-list button", has_text="Retry").click()
        expect(tab.locator("#problems")).to_have_text("Nothing went wrong in the last 7 days.")
        expect(tab.locator("#switch-list button", has_text="Pause searching")).to_be_visible()
        context.close()
    finally:
        server.shutdown()


@pytest.mark.parametrize("width", [1280, 390])
def test_the_find_page_says_how_the_filling_in_of_missing_areas_stands(browser, site, page, width):
    import time

    from leadgen import daily
    day, _ = daily.claim({"location": "876 Fortune Rd, Salt Lake City, UT 84104", "radius": 30})
    fill = {"state": "filling", "left": 4, "areas": 9, "found": 0, "new": 0, "rounds": 1,
            "where": "Layton, West Point, Coalville and the area to the north-west",
            "until": time.time() + 3000}
    daily.finish(day, {"leads": 512, "new": 512, "partial": True, "fill": fill})
    context = _context(browser, viewport={"width": width, "height": 800})
    tab = context.new_page()
    tab.goto(site + "#find")
    note = tab.locator("#fill-note")
    # Which towns are still missing, so staff know the list isn't complete there yet.
    expect(note).to_contain_text("Still filling in 4 areas of the free map data that didn't answer "
                                 "(around Layton, West Point, Coalville and the area to the "
                                 "north-west)")
    expect(tab.locator("#history tbody").first).to_contain_text("Filling in")
    assert tab.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    daily.finish(day, {"leads": 530, "new": 530, "partial": False,
                       "fill": {**fill, "state": "complete", "left": 0, "found": 18, "new": 18}})
    tab.evaluate("loadSearches()")
    expect(note).to_have_text("Complete: the map areas that didn't answer at first were filled "
                              "in later. 18 more businesses were added (18 new).")
    first = tab.locator("#history tbody").first
    expect(first).to_contain_text("530")
    assert "Incomplete" not in first.inner_text() and "Filling in" not in first.inner_text()
    # Stopped by Pause searching: the areas left weren't asked again (not "never answered").
    daily.finish(day, {"fill": {**fill, "state": "stopped", "why": "paused", "left": 2, "found": 18, "new": 18,
                                "where": "Coalville and the area to the north-west"}})
    tab.evaluate("loadSearches()")
    expect(note).to_have_text("Filling in the missing map areas stopped because searching was paused, so 2 areas "
                              "(around Coalville and the area to the north-west) were not asked again. 18 more "
                              "businesses were added (18 new).")
    assert "never answered" not in tab.inner_text("#history")
    context.close()


def test_a_mark_says_saving_until_the_server_has_saved_it(page):
    """On a slow connection the row says "Saving Yes…" (Yes / No disabled) until the server
    answers; only then "Marked by" and "Saved. Moves to …". A failed save says so and the row
    is as it was."""
    held = []
    page.route("**/mark", lambda route: held.append(route))
    row = _row(page, "Costco")
    row.locator(".mark button.yes").click()
    expect(row.locator(".saving")).to_have_text("Saving Yes…")
    assert row.locator(".mark button.yes").is_disabled() and row.locator(".mark button.no").is_disabled()
    page.wait_for_timeout(600)                         # the request is still on its way
    text = row.inner_text()
    assert "Saved." not in text and "Marked by" not in text and "Saving Yes…" in text
    assert page.get_by_role("button", name="Not checked (3)").is_visible()   # nothing moved yet
    held.pop().continue_()
    expect(row).to_contain_text("Saved. Moves to “Has baler or compactor”.")
    expect(row).to_contain_text("Marked by Tester")
    assert row.locator(".saving").count() == 0
    # A save that fails: "Saving No…" and then the row as it was, with the reason.
    page.unroute("**/mark")
    page.route("**/mark", lambda route: held.append(route))
    other = _row(page, "Hampton")
    other.locator(".mark button.no").click()
    expect(other.locator(".saving")).to_have_text("Saving No…")
    held.pop().fulfill(status=503, content_type="application/json",
                       body='{"error": "The saved data isn\'t answering."}')
    expect(page.locator("#toast")).to_contain_text("answer not saved")
    expect(other.locator(".mark-failed")).to_contain_text("No not saved. The saved data isn't answering.")
    expect(other.locator(".saving")).to_have_count(0)
    assert "Marked by" not in other.inner_text() and not other.locator(".mark button.on").count()
    assert other.locator(".mark button.no").is_enabled()


def test_a_mark_that_was_not_saved_stays_on_the_row_with_try_again(page):
    """Offline, a Yes isn't saved: the row says so with Try again (the note at the bottom
    of the screen goes after a few seconds, the row's stays), and once the connection is
    back Try again saves it and the business moves to its tab. Dismiss clears the note."""
    page.context.set_offline(True)
    row = _row(page, "Costco")
    row.locator(".mark button.yes").click()
    failed = row.locator(".mark-failed")
    expect(failed).to_contain_text("Yes not saved. Can't reach the Lead Finder.")
    page.evaluate("hideToast()")                       # as after its few seconds
    expect(failed).to_be_visible()
    assert not row.locator(".mark button.on").count()
    expect(page.get_by_role("button", name="Not checked (3)")).to_be_visible()
    page.context.set_offline(False)
    failed.get_by_role("button", name="Try again to save Yes for Costco Wholesale").click()
    expect(row).to_contain_text("Saved. Moves to “Has baler or compactor”.")
    expect(row.locator(".mark-failed")).to_have_count(0)
    expect(page.get_by_role("button", name="Has baler or compactor (1)")).to_be_visible()
    # Dismissed instead: the note goes, nothing is saved, the focus is back on the row's Yes.
    page.context.set_offline(True)
    other = _row(page, "Hampton")
    other.locator(".mark button.no").click()
    expect(other.locator(".mark-failed")).to_contain_text("No not saved.")
    page.context.set_offline(False)
    other.get_by_role("button", name="Dismiss: No not saved for Hampton Inn").click()
    expect(other.locator(".mark-failed")).to_have_count(0)
    assert not other.locator(".mark button.on").count()
    assert page.evaluate("document.activeElement.dataset.ctl") == "yes"


def test_a_place_outside_arcos_area_is_named_and_needs_a_second_yes(page, monkeypatch):
    from leadgen import pipeline
    from leadgen.pipeline import RunResult
    monkeypatch.setattr(pipeline, "geocode", lambda location, key: (45.5152, -122.6784, "Portland, Oregon"))
    monkeypatch.setattr(web.finding, "run", lambda params, progress: RunResult(
        [], params.center, params.place, [], {"leads kept": 0, "seconds": 3.2}))
    sent = _posts(page, "/search")
    page.click("nav a[data-page=find]")
    page.wait_for_selector("text=Today's is available")
    page.fill("input[name=location]", "Portland")
    page.click("#go")
    confirm = page.locator("#confirm-dlg")
    expect(confirm).to_be_visible()
    assert "Portland, Oregon" in confirm.inner_text() and "633 miles" in confirm.inner_text()
    expect(page.locator("#confirm-far")).to_contain_text("outside Arco's area")
    page.click("#confirm-go")                          # "Next": the second question
    far = page.locator("#far-dlg")
    expect(far).to_be_visible()
    assert "“Portland, Oregon” is 633 miles from Arco's shop" in far.inner_text()
    assert page.evaluate("document.activeElement.id") == "far-back"   # the safe choice
    page.click("#far-back")
    assert not far.is_visible() and not sent
    page.click("#go")
    expect(confirm).to_be_visible()
    page.click("#confirm-go")
    expect(far).to_be_visible()
    page.click("#far-go")
    page.wait_for_selector("#done-card:not([hidden])")
    assert len(sent) == 1
    where = page.locator("#history td.h-where").first
    expect(where).to_contain_text("Searched around Portland, Oregon · 633 miles from Arco")


def test_the_place_found_is_named_before_a_search_in_the_area(page, monkeypatch):
    from leadgen import geo
    monkeypatch.setattr(geo, "request_json", lambda *a, **k: [])     # the lookups find nothing
    page.click("nav a[data-page=find]")
    page.wait_for_selector("text=Today's is available")
    page.fill("input[name=location]", "Murray")
    page.click("#go")
    expect(page.locator("#confirm-list")).to_contain_text("Murray, UT")
    assert re.search(r"From Arco's shop\s+5\.\d miles", page.inner_text("#confirm-list"))
    assert page.locator("#confirm-far").is_hidden() and page.inner_text("#confirm-go") == "Start search"
    page.click("#confirm-back")
    page.fill("input[name=location]", "Nowhereville zz")
    page.click("#go")
    expect(page.locator("#err-location")).to_contain_text("Could not find the place")
    assert not page.locator("#confirm-dlg").is_visible()


def test_show_more_fetches_the_next_page_only(browser, site, page):
    extra = []
    for i in range(130):
        lead = Lead(name=f"Warehouse {i:03}", lat=40.6 + i * 0.001, lon=-111.95, source="osm",
                    source_id=f"w{i}", raw_categories=["building=warehouse"])
        score_lead(lead, config.DEFAULT_KEYWORDS)
        extra.append(lead)
    saved.save_search(extra, config.DEFAULT_KEYWORDS)
    page.reload()
    page.wait_for_selector("table.leads")
    rows = page.locator("table.leads tbody tr")
    expect(rows).to_have_count(100)
    with page.expect_response(lambda r: "/leads?" in r.url and "offset=100" in r.url) as info:
        page.click("#leads-more")
    body = info.value.json()
    left = body["total"] - 100
    assert left > 20 and len(body["leads"]) == left       # only the rows not shown yet
    expect(rows).to_have_count(body["total"])
    assert page.locator("#leads-more").is_hidden()
    names = page.locator("table.leads tbody tr td.c-name strong").all_inner_texts()
    assert len(set(names)) == body["total"]


def test_the_mark_column_is_only_as_wide_as_it_needs_on_a_laptop(browser, site, page):
    _row(page, "Smith").locator(".mark button.yes").click()
    page.get_by_role("button", name="Has baler or compactor (1)").click()
    _row(page, "Smith").get_by_role("button", name="Just called").click()
    page.locator("#outcomes").get_by_role("button", name="Follow Up").click()
    page.click("#call-save")
    page.wait_for_selector("#call-dlg:not([open])", state="attached")
    page.set_viewport_size({"width": 1440, "height": 900})
    widths = page.evaluate("""() => { const th = [...document.querySelectorAll('table.leads thead th')];
        return th.map((e) => Math.round(e.getBoundingClientRect().width)); }""")
    assert widths[0] <= 180, widths                    # Baler? / Call
    cell = _row(page, "Smith").locator("td.c-mark")
    box = cell.bounding_box()
    for button in cell.locator("button").all():         # every button still fits the column
        b = button.bounding_box()
        assert b["x"] + b["width"] <= box["x"] + box["width"] + 1


def test_the_tier_d_note_is_true_whatever_minimum_score_was_used(page):
    weak = Lead(name="Acme Office", lat=40.7, lon=-111.9, source="osm", source_id="weak",
                raw_categories=["office=company"])
    score_lead(weak, config.DEFAULT_KEYWORDS)
    assert weak.tier == "D"
    _row(page, "Costco").locator(".mark button.yes").click()
    page.wait_for_selector("#recent:not([hidden])")
    page.click("nav a[data-page=stats]")
    expect(page.locator("#tier-d-note")).to_contain_text("Tier D (scores below 20) is empty")
    saved.save_search([weak], config.DEFAULT_KEYWORDS)  # a search with a lower minimum score
    page.click("nav a[data-page=leads]")
    page.click("nav a[data-page=stats]")
    expect(page.locator("#tier-d-note")).to_contain_text("1 saved lead is in tier D (scores below 20)")
    assert "usually empty" not in page.inner_text("#tier-d-note")


# ---- review round 15: keyboard focus, clicks as the list moves, phone navigation, call notes,
# the town of a lead without an address, the running search in the history

def _in_list(page):
    return page.evaluate("document.getElementById('leads-wrap').contains(document.activeElement)"
                         " && document.activeElement.id !== 'leads-wrap'")


def test_marking_with_the_keyboard_keeps_the_focus_in_the_list(page):
    first = page.locator("table.leads tbody tr").first
    name = first.locator("td.c-name strong").inner_text()
    first.locator(".mark button.yes").focus()
    page.keyboard.press("Enter")
    expect(page.locator("#toast")).to_contain_text(f"{name}: marked Yes.")
    assert _in_list(page)
    assert page.evaluate("document.activeElement.closest('tr').dataset.key") == \
        first.get_attribute("data-key")
    page.keyboard.press("Tab")
    assert _in_list(page)
    # Undo from the keyboard: the focus stays on that business (its Yes button again).
    page.locator("table.leads tbody tr").first.locator("button.undo").focus()
    page.keyboard.press("Enter")
    expect(page.locator("#toast")).to_contain_text("undone")
    page.wait_for_function("!document.querySelector('table.leads tbody tr button.undo')")
    assert _in_list(page)


def test_a_click_just_as_the_list_moves_is_never_silently_dropped(page):
    row = _row(page, "Hampton")
    page.evaluate("S.shiftedAt = Date.now()")             # the rows have just moved up
    row.locator(".mark button.yes").click()
    expect(page.locator("#toast")).to_contain_text("Nothing was saved for Hampton Inn")
    expect(page.locator("#toast")).to_contain_text("click Yes again")
    assert not row.locator(".mark button.yes.on").count()
    page.wait_for_timeout(800)
    row.locator(".mark button.yes").click()
    expect(page.locator("#toast")).to_contain_text("Hampton Inn: marked Yes.")


@pytest.mark.parametrize("width", [375, 390])
def test_phone_navigation_is_easy_to_tap(browser, site, page, width):
    context = _context(browser, viewport={"width": width, "height": 800})     # no touch screen needed
    phone = context.new_page()
    phone.goto(site + "#leads")
    phone.wait_for_selector("table.leads")
    sizes = phone.evaluate("""() => [...document.querySelectorAll('nav a'), document.getElementById('menu-btn')]
        .map((e) => [e.textContent.trim().slice(0, 20), e.getBoundingClientRect()])
        .map(([t, r]) => [t, Math.round(r.width), Math.round(r.height), Math.round(r.right)])""")
    context.close()
    assert len(sizes) == 5
    assert all(w >= 44 and h >= 44 and right <= width for _, w, h, right in sizes), sizes


def test_call_notes_say_when_the_limit_is_reached(page):
    _row(page, "Smith").get_by_role("button", name="Just called").click()
    count = page.locator("#call-notes-count")
    page.fill("#call-notes", "x" * 4000)
    expect(count).to_be_hidden()
    page.fill("#call-notes", "x" * 4800)
    expect(count).to_have_text("200 characters left (5,000 at most).")
    page.fill("#call-notes", "")
    # A long email pasted in: only 5,000 characters fit, and the box says so before Save.
    page.evaluate("""() => {
        const box = document.getElementById('call-notes'), dt = new DataTransfer();
        dt.setData('text/plain', 'y'.repeat(5200));
        box.focus();
        box.dispatchEvent(new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true }));
        box.value = 'y'.repeat(5000);
        box.dispatchEvent(new Event('input', { bubbles: true }));
    }""")
    expect(count).to_contain_text("Limit reached")
    expect(count).to_contain_text("the last 200 characters you pasted were left out")
    page.fill("#call-notes", "z" * 5000)
    expect(count).to_contain_text("Limit reached: a summary can be up to 5,000 characters.")


def test_a_lead_without_an_address_shows_the_town_it_is_near(page):
    lead = Lead(name="Lowe's Home Improvement", lat=40.6097, lon=-111.9391, source="osm",
                source_id="node/9", raw_categories=["shop=doityourself"],
                map_url="https://www.openstreetmap.org/node/9")
    score_lead(lead, config.DEFAULT_KEYWORDS)
    saved.save_search([lead], config.DEFAULT_KEYWORDS)
    page.reload()
    page.fill("#filter", "lowe")
    row = _row(page, "Lowe's")
    expect(row.locator(".contact")).to_contain_text("No street address · near West Jordan, UT 84088")
    expect(row.locator(".contact a.map")).to_have_count(1)


def test_the_history_shows_todays_search_while_it_runs(page, monkeypatch):
    from leadgen.pipeline import RunResult
    go = threading.Event()

    def run(params, progress):
        go.wait(20)
        return RunResult([], params.center, params.place, [], {"leads kept": 0, "seconds": 1})
    monkeypatch.setattr(web.finding, "run", run)
    page.click("nav a[data-page=find]")
    page.wait_for_selector("text=Today's is available")
    expect(page.locator("#history")).to_contain_text("No searches yet.")
    page.fill("input[name=location]", "Murray")
    page.click("#go")
    page.click("#confirm-go")
    page.wait_for_selector("#progress-card:not([hidden])")
    expect(page.locator("#history .h-leads")).to_contain_text("Running…")
    go.set()
    page.wait_for_selector("#done-card:not([hidden])")
    expect(page.locator("#history .h-leads")).not_to_contain_text("Running…")


def test_saving_a_call_from_the_keyboard_keeps_the_focus_on_that_business(page):
    row = _row(page, "Hampton")
    key = row.get_attribute("data-key")
    row.get_by_role("button", name="Just called").focus()
    page.keyboard.press("Enter")
    expect(page.locator("#call-dlg")).to_be_visible()
    page.keyboard.type("Left a message.")
    page.locator("#outcomes").get_by_role("button", name="No Contact").click()
    page.locator("#call-save").focus()
    page.keyboard.press("Enter")
    expect(page.locator("#toast")).to_contain_text("Hampton Inn: saved as No Contact.")
    assert _in_list(page)
    assert page.evaluate("document.activeElement.closest('tr').dataset.key") == key


def test_a_search_cut_off_by_a_restart_says_what_was_saved_and_frees_the_day(browser, monkeypatch):
    """The server restarts mid-search (a deploy): the page notices, what the search had
    found is saved, the history says so, and Find leads is available again at once."""
    from werkzeug.serving import make_server

    from leadgen import interrupted
    from leadgen.sources import report_found

    found = [Lead(name=name, lat=40.66 + i * 0.01, lon=-111.89, source="osm", source_id=f"way/{i}",
                  raw_categories=[tag])
             for i, (name, tag) in enumerate([("Costco", "shop=wholesale"),
                                              ("Smith's Marketplace", "shop=supermarket")])]
    reported, gate = threading.Event(), threading.Event()

    def run(params, progress):
        report_found(list(found))
        reported.set()
        gate.wait(30)
        raise RuntimeError("the old server's search ends with the test")
    monkeypatch.setattr(web.finding, "run", run)
    app = web.create_app()
    server = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    context = _context(browser, viewport={"width": 1280, "height": 900})
    tab = context.new_page()
    errors = []
    tab.on("pageerror", lambda exc: errors.append(str(exc)))
    try:
        tab.goto(f"http://127.0.0.1:{server.server_port}/#find")
        tab.wait_for_selector("text=Today's is available")
        tab.fill("input[name=location]", "Murray")
        tab.click("#go")
        tab.click("#confirm-go")
        tab.wait_for_selector("#progress-card:not([hidden])")
        assert reported.wait(10)
        # The restart: the old process says its search ended; the new one has no jobs.
        interrupted._shutting_down()
        interrupted._live.clear()
        app.extensions["leadgen"].jobs.clear()
        expect(tab.locator("#history .h-leads")).to_contain_text("2 Interrupted", timeout=15000)
        expect(tab.locator("#history")).to_contain_text(
            "the 2 businesses it had found were saved. It didn't use up the day's search.")
        expect(tab.locator("#day-note")).to_contain_text("cut off by a server restart")
        expect(tab.locator("#go")).to_be_enabled()
        assert sorted(l.name for l in saved.load()) == ["Costco", "Smith's Marketplace"]
    finally:
        gate.set()
        context.close()
        server.shutdown()
    assert not errors


def _history_with_searches():
    from leadgen import daily
    day, _ = daily.claim({"location": "876 Fortune Rd, Salt Lake City, UT 84104", "radius": 30,
                          "place": "Arco Compactor, 876 Fortune Rd, Salt Lake City, UT 84104"})
    daily.finish(day, {"leads": 1038, "new": 1038, "details": {"leads kept": 1038}})


@pytest.mark.parametrize("width", [375, 1280, 1440, 1920, 2560])
def test_search_history_headings_and_details_are_whole_words(browser, site, page, width):
    _history_with_searches()
    context = _context(browser, viewport={"width": width, "height": 900})
    tab = context.new_page()
    tab.goto(site + "#find")
    tab.wait_for_selector("#history table.hist")
    one_line = tab.evaluate("""() => {
        const lines = (e) => { const s = getComputedStyle(e); const r = document.createRange();
            r.selectNodeContents(e); const tops = new Set([...r.getClientRects()].map((x) => Math.round(x.top)));
            return tops.size; };
        const heads = [...document.querySelectorAll('#history thead th')].filter((t) => t.textContent && t.offsetWidth);
        const details = [...document.querySelectorAll('#history td.h-act button')];
        return { heads: heads.map((t) => [t.textContent, lines(t)]), details: details.map((b) => lines(b)),
                 page: document.documentElement.scrollWidth > window.innerWidth }; }""")
    assert all(n == 1 for _, n in one_line["heads"]), one_line
    assert one_line["details"] and all(n == 1 for n in one_line["details"]), one_line
    assert not one_line["page"]
    context.close()


# A 30-mile search around Arco whose map data answered only partly (the numbers of a real one).
REAL_DETAILS = {"osm raw results": 795, "osm areas searched": "about 2 of 9 areas",
                "osm areas asked again": "7 (some never answered)",
                "osm areas filled in later": "559 businesses (554 new), 6 areas still missing "
                                             "(around Kaysville, Farmington, Centerville and more)",
                "results in radius": 795, "duplicates merged": 49, "after dedupe": 746,
                "below min score": 328, "min score": 20, "leads kept": 418, "competitors flagged": 2,
                "tier A": 31, "tier B": 102, "tier C": 285, "tier D": 0, "seconds": 412}


@pytest.mark.parametrize("width", [320, 390, 768, 1100, 1440, 1920, 2560])
def test_search_details_show_every_value_whole(browser, site, page, width):
    """The search history's Details: every number reads as one figure on one line ("795",
    never "7 / 9 / 5"), and no label or value breaks inside a word, at every width."""
    from leadgen import daily
    day, _ = daily.claim({"location": "876 Fortune Rd, Salt Lake City, UT 84104", "radius": 30})
    daily.finish(day, {"leads": 418, "new": 418, "details": REAL_DETAILS,
                       "warnings": ["The map areas that didn't answer were asked again in the background "
                                    "(some never answered)."]})
    context = _context(browser, viewport={"width": width, "height": 900})
    tab = context.new_page()
    tab.goto(site + "#find")
    tab.get_by_role("button", name="Details").first.click()
    tab.wait_for_selector("dl.details")
    found = tab.evaluate("""() => {
        const tops = (range) => new Set([...range.getClientRects()].filter((r) => r.width)
                                         .map((r) => Math.round(r.top))).size;
        // Each word of the element's text sits on one line (a word split across lines has two tops).
        const split = (e) => {
          const out = [];
          for (const node of e.childNodes) {
            if (node.nodeType !== 3) continue;
            for (const m of node.textContent.matchAll(/\\S+/g)) {
              const r = document.createRange(); r.setStart(node, m.index); r.setEnd(node, m.index + m[0].length);
              if (tops(r) > 1) out.push(m[0]);
            }
          }
          return out;
        };
        const lines = (e) => { const r = document.createRange(); r.selectNodeContents(e); return tops(r); };
        const items = [...document.querySelectorAll('dl.details > div')];
        return { split: items.flatMap((d) => [...d.children].flatMap(split)),
                 numbers: items.map((d) => d.querySelector('dd'))
                               .filter((dd) => /^[\\d,]+$/.test(dd.textContent))
                               .map((dd) => [dd.textContent, lines(dd)]),
                 page: document.documentElement.scrollWidth > window.innerWidth }; }""")
    assert not found["split"], found
    assert ("795", 1) in [tuple(n) for n in found["numbers"]]
    assert all(n == 1 for _, n in found["numbers"]), found["numbers"]
    assert not found["page"]
    context.close()


@pytest.mark.parametrize("width", [375, 390])
def test_calls_results_are_one_list_on_a_phone(browser, site, page, width):
    from leadgen import calls
    leads = saved.load()
    calls.log_call(leads[0].uid, "Follow Up", "Call back Monday", by="Dana")
    calls.log_call(leads[1].uid, "Interested", "Wants a quote", by="Dana")
    context = _context(browser, viewport={"width": width, "height": 800})
    tab = context.new_page()
    tab.goto(site + "#calls")
    tab.wait_for_selector("table.plain.calls")
    expect(tab.locator("#call-tabs")).to_be_hidden()
    pick = tab.locator("#call-pick")
    expect(pick).to_be_visible()
    assert pick.locator("option").all_inner_texts()[:3] == [
        "All called businesses: 2", "Interested: 1", "Follow Up: 1"]
    box = pick.bounding_box()
    assert box["height"] < 60                       # one row
    pick.select_option("Follow Up")
    expect(tab.locator("#calls-wrap")).not_to_contain_text("Wants a quote")
    expect(tab.locator("#calls-wrap")).to_contain_text("Call back Monday")
    assert "Follow" in tab.evaluate("location.hash")
    assert tab.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    context.close()


@pytest.mark.parametrize("width", [721, 768, 820])
def test_the_bar_takes_at_most_two_rows_on_a_tablet(browser, site, page, width):
    context = _context(browser, viewport={"width": width, "height": 1000})
    tab = context.new_page()
    tab.goto(site + "#calls")
    tab.wait_for_selector("nav a[data-page=calls]")
    rows = tab.evaluate("""() => {
        const parts = [document.querySelector('.brand'), document.querySelector('#menu-btn'),
                       ...document.querySelectorAll('nav a')].filter((e) => e.offsetWidth);
        const tops = [...new Set(parts.map((e) => Math.round(e.getBoundingClientRect().top / 20)))];
        return tops.length; }""")
    assert rows <= 2
    expect(tab.locator("#side-bottom")).to_be_hidden()
    # Your name, the colours and the tutorial are under Menu.
    tab.click("#menu-btn")
    expect(tab.locator("#side-bottom")).to_be_visible()
    expect(tab.locator("#tour-btn")).to_be_visible()
    assert tab.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    context.close()


def test_the_browser_bar_colour_follows_the_chosen_theme(browser, site, page):
    context = _context(browser, viewport={"width": 390, "height": 800}, color_scheme="light")
    tab = context.new_page()
    tab.goto(site + "#find")
    tab.wait_for_selector("#menu-btn")
    bar = "document.querySelector('meta[name=theme-color][media=\"(prefers-color-scheme: light)\"]').content"
    assert tab.evaluate(bar) == "#15304d"
    tab.click("#menu-btn")
    tab.click("[data-theme-pick=dark]")
    assert tab.evaluate(bar) == "#0b1220"
    tab.reload()
    tab.wait_for_selector("#menu-btn")
    assert tab.evaluate(bar) == "#0b1220"           # kept after a reload
    tab.click("#menu-btn")
    tab.click("[data-theme-pick=system]")
    assert tab.evaluate(bar) == "#15304d"
    context.close()


def test_why_this_score_and_explain_are_in_plain_words(browser, site, page):
    """A lead decided by each classifying rule: its "Why this score" and "Explain" say
    in a salesperson's words how its type is known and what to check, never a rule's
    name or the map's own terms."""
    import copy

    from test_classify_rules import RULE_EXAMPLES, internal_words
    leads = []
    for i, (_, example, _) in enumerate(RULE_EXAMPLES):
        lead = copy.deepcopy(example)
        lead.lat += 0.05 + i * 0.02
        leads.append(score_lead(lead, config.DEFAULT_KEYWORDS))
    saved.save_search(leads, config.DEFAULT_KEYWORDS)
    page.goto(site + "#leads?tab=all")
    expect(page.locator("table.leads tbody tr")).to_have_count(len(RULE_EXAMPLES) + 4)
    for details in page.locator("td.c-why details.explain").all():
        details.locator("summary").click()
    texts = page.eval_on_selector_all("td.c-why", "(cells) => cells.map((c) => c.innerText)")
    assert len(texts) == len(RULE_EXAMPLES) + 4
    for text in texts:
        assert not internal_words(text), text
    acme = _row(page, "Acme Industries").locator("td.c-why")
    expect(acme).to_contain_text("Only the building type or the business name suggests what it does — "
                                 "confirm before calling.")
    expect(acme).to_contain_text("(its type is taken from the map listing).")
