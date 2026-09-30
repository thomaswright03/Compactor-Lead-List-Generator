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
    expect(page.locator("#go-error")).to_contain_text("Use at most 20 search words")
    expect(page.locator("#err-keywords")).to_be_visible()
    page.wait_for_timeout(300)                         # the search history has reloaded by now
    expect(page.locator("#go-error")).to_contain_text("Use at most 20 search words")
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
                 parts: [...document.querySelectorAll('#calls-wrap td.notes, #calls-wrap .call-cell button')].map(inside),
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
