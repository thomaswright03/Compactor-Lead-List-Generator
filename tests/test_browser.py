"""The pages in a real browser: marks, undo, calls, stats and downloads.

Needs Playwright and its Chromium (pip install playwright; playwright install
chromium). Skipped when they are missing. LEADGEN_CHROMIUM can point at a
Chromium binary to use instead of Playwright's own.
"""

import contextlib
import os
import re
import threading

import pytest

from leadgen import config, saved, web
from leadgen.models import Lead
from leadgen.scoring import score_lead

sync_api = pytest.importorskip("playwright.sync_api")
expect = sync_api.expect

# Any address but this machine's (the site under test).
OUTSIDE = re.compile(r"^(?!https?://(127\.0\.0\.1|localhost)[:/])[a-z]+://")


def _context(browser, name="Tester", first_visit=False, **kw):
    """A browser window whose "Your name" is already set (so a first Yes / No doesn't ask),
    and which was offered the tutorial before (first_visit=True: it never was)."""
    context = browser.new_context(**kw)
    # Nothing leaves this machine: the Map page's street map (OpenStreetMap's tiles) is
    # refused, as it is offline, and the page draws the rest without it.
    context.route(OUTSIDE, lambda route: route.abort())
    if name:
        context.add_init_script(f"try {{ localStorage.setItem('my-name', {name!r}); }} catch (e) {{}}")
    if not first_visit:
        context.add_init_script("try { localStorage.setItem('tour-offered', '1'); } catch (e) {}")
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


def test_a_verified_phone_and_contact_show_beside_the_listings_phone(page):
    row = _row(page, "Costco")
    expect(row.locator("td.contact")).to_contain_text("(801) 555-0101")
    row.get_by_role("button", name="Add verified phone or contact: Costco Wholesale").click()
    expect(page.locator("#contact-dlg")).to_be_visible()
    expect(page.locator("#contact-listed")).to_contain_text("(801) 555-0101")
    # A number without its area code is refused, and the box stays open with what was typed.
    page.fill("#contact-phone", "555-0199")
    page.fill("#contact-name", "Jane Doe, facilities manager")
    page.click("#contact-save")
    expect(page.locator("#contact-error")).to_contain_text("area code")
    page.fill("#contact-phone", "801-555-0199")
    page.click("#contact-save")
    expect(page.locator("#contact-dlg")).to_be_hidden()
    box = _row(page, "Costco").locator(".verified")
    expect(box).to_contain_text("Verified (801) 555-0199 · Ask for Jane Doe, facilities manager")
    expect(box).to_contain_text("Saved by Tester")
    assert box.locator('a[href="tel:8015550199"]').count() == 1
    # The listing's own phone is still there, and the save is kept after a reload.
    expect(_row(page, "Costco").locator("td.contact")).to_contain_text("(801) 555-0101")
    page.reload()
    page.wait_for_selector("table.leads")
    expect(_row(page, "Costco").locator(".verified")).to_contain_text("Ask for Jane Doe")
    expect(_row(page, "Costco").get_by_role("button", name="Edit verified contact: Costco Wholesale")).to_be_visible()
    # Saved again with another number: Earlier versions lists both, with who saved them.
    _row(page, "Costco").get_by_role("button", name="Edit verified contact: Costco Wholesale").click()
    expect(page.locator("#contact-phone")).to_have_value("(801) 555-0199")
    page.fill("#contact-phone", "801-555-0123")
    page.click("#contact-save")
    expect(_row(page, "Costco").locator(".verified")).to_contain_text("(801) 555-0123")
    _row(page, "Costco").get_by_role("button", name="Earlier verified contacts: Costco Wholesale").click()
    expect(page.locator("#hist-list")).to_contain_text("Verified phone and contact")
    expect(page.locator("#hist-list .item", has_text="(801) 555-0199 · ask for Jane Doe")).to_contain_text("by Tester")
    expect(page.locator("#hist-list .item", has_text="(801) 555-0123")).to_have_count(1)


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
    # The place the search will actually run around, and how far it is from AARCO's shop.
    assert "AARCO Waste & Disposal, 876 Fortune Rd" in text and "0.0 miles" in text
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
    assert page.title() == "Leads · AARCO Waste & Disposal Lead Finder"
    assert "whatever its answer" in page.inner_text("#call-hint")
    page.click("nav a[data-page=calls]")
    page.wait_for_function("document.title === 'Calls · AARCO Waste & Disposal Lead Finder'")
    expect(page.locator("#calls-wrap")).to_contain_text("Log a call with Just called")
    page.click("nav a[data-page=stats]")
    page.wait_for_function("document.title === 'Stats · AARCO Waste & Disposal Lead Finder'")
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


# Every control on a business's row the selector picks that is under 44 px tall or wide.
SMALL_TARGETS = """(sel) => [...document.querySelectorAll(sel)]
    .filter((e) => e.offsetParent !== null)
    .map((e) => [e.textContent.trim().slice(0, 30), e.getBoundingClientRect()])
    .filter(([, r]) => r.width > 0 && (r.height < 44 || r.width < 44))
    .map(([t, r]) => `${t}: ${Math.round(r.width)}x${Math.round(r.height)}`)"""


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [320, 390, 768, 1100, 1440, 2560])
def test_row_buttons_are_44px_targets_with_a_mouse_at_every_width(browser, site, page, width, theme):
    """Yes, No, Just called, History, the Undos, the verified-contact and Why this score buttons,
    the column sorts, Explain and the row's links: 44 px each way in a mouse window too (a narrow
    one is as hard to hit as a phone), on Leads and on Calls, and the rows still fit the window."""
    from leadgen import calls
    calls.log_call(_uid("Costco"), "Follow Up", "Call back Friday.", by="Dana")
    context = _context(browser, viewport={"width": width, "height": 900}, color_scheme=theme)   # no touch screen
    tab = context.new_page()
    tab.goto(site + "#leads")
    tab.wait_for_selector("table.leads")
    assert tab.evaluate("matchMedia('(pointer: coarse)').matches") is False
    _row(tab, "Smith").locator(".mark button.yes").click()           # its Undo appears
    tab.wait_for_selector("table.leads button.undo")
    small = tab.evaluate(SMALL_TARGETS, "table.leads button, table.leads a, table.leads summary")
    wide = tab.evaluate("document.documentElement.scrollWidth > window.innerWidth")
    tab.click("nav a[data-page=calls]")
    tab.wait_for_selector("table.calls button.undo")
    small += tab.evaluate(SMALL_TARGETS, "table.calls button, table.calls a")
    wide = wide or tab.evaluate("document.documentElement.scrollWidth > window.innerWidth")
    context.close()
    assert small == [] and not wide, (small, wide)


def test_competitors_are_flagged_not_asked(page):
    assert "Pro Baler" not in page.inner_text("#leads-wrap")      # not in Not checked
    assert page.inner_text("#n-leads") == "3 to check"
    page.get_by_role("button", name="Competitors & AARCO (1)").click()
    row = _row(page, "Pro Baler")
    expect(row).to_contain_text("Competitor: not a prospect")
    assert row.locator(".mark button").count() == 0
    assert "aren't asked Yes / No" in page.inner_text("#competitor-hint")


def test_aarcos_and_its_parent_companys_listings_are_flagged_with_the_names_now(page):
    """AARCO's own listing saved under the company's old short name, and its parent
    company's facility saved before it was recognised, show on the Competitors & AARCO tab with
    the names config has now, never in Not checked; "Ace Hardware" stays a prospect."""
    import json

    from leadgen import store
    own = Lead(name="AARCO Compactor", lat=40.74, lon=-111.94, source="osm", source_id="own",
               raw_categories=["building=industrial"])
    ace = Lead(name="Recycling Center", lat=40.73, lon=-111.93, source="osm", source_id="ace",
               raw_categories=["amenity=recycling"], address="1240 S Wallace Rd", city="Salt Lake City")
    hardware = Lead(name="Ace Hardware", lat=40.75, lon=-111.9, source="osm", source_id="hw",
                    raw_categories=["shop=hardware"], address="5 State St", city="Salt Lake City")
    for lead in (own, ace, hardware):
        score_lead(lead, config.DEFAULT_KEYWORDS)
    saved.save_search([own, ace, hardware], config.DEFAULT_KEYWORDS)
    with store.connect() as db:          # as saved by earlier versions
        for uid, flags, kind in ((own.uid, ["OWN COMPANY: AARCO Compactor"], "Own company"),
                                 (ace.uid, [], "Waste / recycling facility")):
            data = json.loads(db.one("SELECT lead FROM leads WHERE uid = ?", (uid,))[0])
            data.update(flags=flags, lead_type=kind)
            db.run("UPDATE leads SET lead = ?, last_seen = last_seen + 1 WHERE uid = ?", (json.dumps(data), uid))
    page.reload()
    page.wait_for_selector("table.leads")
    listed = page.inner_text("#leads-wrap")
    assert "Ace Hardware" in listed and "Recycling Center" not in listed and "AARCO Compactor" not in listed
    page.get_by_role("button", name="Competitors & AARCO (3)").click()
    own_row, ace_row = _row(page, "AARCO Compactor"), _row(page, "Recycling Center")
    expect(own_row).to_contain_text("OWN COMPANY: AARCO Waste & Disposal")
    expect(own_row).to_contain_text("AARCO's own listing")
    expect(ace_row).to_contain_text("OWN COMPANY: AARCO's parent company (ACE Recycling & Disposal)")
    expect(ace_row).to_contain_text("AARCO's parent company")
    assert ace_row.locator(".mark button").count() == 0
    # Neither is given a prospect's score or tier, and Stats names them as AARCO's.
    for row in (own_row, ace_row):
        expect(row.locator("td.score")).to_have_text("Not scored")
    page.click("nav a[data-page=stats]")
    expect(page.locator("#stats-left-out")).to_contain_text(
        "1 competitor and 2 AARCO listings are left out of these numbers")


def test_the_brand_shows_the_companys_name_and_fits_on_a_phone(browser, site):
    """The brand block names AARCO Waste & Disposal; on a narrow phone, where that
    doesn't fit beside Menu, it says AARCO, and nothing is cut off."""
    for width, name in ((1440, "AARCO Waste & Disposal"), (390, "AARCO"), (320, "AARCO")):
        context = _context(browser, viewport={"width": width, "height": 800})
        tab = context.new_page()
        tab.goto(site + "#find")
        brand = tab.locator(".side .brand")
        expect(brand).to_contain_text("Lead Finder")
        assert brand.evaluate("(b) => b.scrollWidth <= b.clientWidth"), width
        shown = brand.evaluate("(b) => [...b.querySelectorAll('strong')].filter((s) => s.offsetParent)"
                               ".map((s) => s.textContent)")
        assert shown == [name], (width, shown)
        assert tab.title() == "Find leads · AARCO Waste & Disposal Lead Finder"
        context.close()


def test_a_note_never_follows_to_another_page(page):
    _row(page, "Costco").locator(".mark button.yes").click()
    expect(page.locator("#toast")).to_be_visible()
    page.click("nav a[data-page=stats]")
    expect(page.locator("#toast")).to_be_hidden()
    assert "Costco Wholesale: marked Yes" in page.inner_text("#recent")   # the Undo is still there


def test_every_page_opens_at_the_top(page):
    # #map is also the map's id; the browser must not jump down to it.
    page.set_viewport_size({"width": 390, "height": 500})
    for target in ["map", "stats", "find", "map", "calls", "leads"]:
        page.evaluate("window.scrollTo(0, 400)")
        page.click(f"nav a[data-page={target}]")
        expect(page.locator(f"#page-{target}")).to_be_visible()
        page.wait_for_timeout(50)
        assert page.evaluate("window.scrollY") == 0, target
    for target in ["map", "stats", "leads"]:
        page.goto(page.url.split("#")[0] + "#" + target)
        expect(page.locator(f"#page-{target}")).to_be_visible()
        page.wait_for_timeout(300)
        assert page.evaluate("window.scrollY") == 0, "opened at #" + target
    fresh = page.context.new_page()
    fresh.set_viewport_size({"width": 390, "height": 500})
    fresh.goto(page.url.split("#")[0] + "#map")
    expect(fresh.locator("#page-map")).to_be_visible()
    fresh.wait_for_timeout(500)
    assert fresh.evaluate("window.scrollY") == 0, "a new tab at #map"
    fresh.close()


def test_stats_show_only_the_note_until_something_is_marked(page):
    page.click("nav a[data-page=stats]")
    page.wait_for_selector("#stats-empty:not([hidden])")
    assert page.locator("#stats-data").is_hidden()
    assert "1 competitor is left out of these numbers" in page.inner_text("#stats-left-out")
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


def test_how_far_stops_at_100_miles_as_it_is_typed(page):
    """Typed on the keyboard, a number past a field's most stops at the most with a note,
    before Find leads is pressed (the owner, 2026-10-08)."""
    page.click("nav a[data-page=find]")
    page.wait_for_selector("text=Today's is available")
    box = page.locator("input[name=radius]")
    box.fill("")
    box.press_sequentially("9000000")
    expect(box).to_have_value("100")
    expect(page.locator("#err-radius")).to_have_text("How far can be at most 100 miles.")
    box.fill("")
    box.press_sequentially("45")
    expect(box).to_have_value("45")
    expect(page.locator("#err-radius")).to_have_count(0)


def test_find_leads_says_why_a_search_cannot_start(page):
    page.click("nav a[data-page=find]")
    page.wait_for_selector("text=Today's is available")
    # The limits are under "More about these fields", not in the one-line hint.
    assert "up to 20 words" not in page.inner_text("#form")
    page.click("#find-more summary")
    assert "Up to 20 words, each up to 60 characters" in page.inner_text("#find-more")
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
    # An empty "Search around" is never quietly replaced by AARCO's address.
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
    assert fits["nav"] == [True] * 5                         # Stats is never cut off
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
    tab.get_by_role("button", name="Competitors & AARCO (0)").click()
    expect(tab.locator("#leads-wrap .empty")).to_contain_text(
        "No Pro Baler, Action Compaction, AARCO Waste & Disposal or ACE Recycling & Disposal listings found "
        "in the saved leads yet.")
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
                 "about 8 of the 9 areas", "didn't use up the day's search"):
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


@pytest.mark.parametrize("touch", [True, False])
def test_a_link_in_a_banner_is_easy_to_tap_on_a_phone(browser, site, page, touch):
    from leadgen import daily
    day, _ = daily.claim({"location": "84101"})
    daily.finish(day, {"leads": 3})
    context = _context(browser, viewport={"width": 390 if touch else 1280, "height": 844},
                       has_touch=touch, is_mobile=touch)
    tab = context.new_page()
    tab.goto(site + "#find")
    link = tab.locator("#form-closed a", has_text="Leads")
    expect(link).to_be_visible()
    banner = tab.evaluate("document.getElementById('form-closed').getBoundingClientRect().height")
    tap = link.bounding_box()["height"]
    tab.add_style_tag(content="#form-closed a { padding-block: 0 !important; }")
    plain = tab.evaluate("document.getElementById('form-closed').getBoundingClientRect().height")
    context.close()
    # A 44px tap area on a touch screen, a plain line of text with a mouse; the banner's
    # lines don't spread apart either way.
    assert tap >= 44 if touch else tap < 30, tap
    assert banner == plain


@pytest.mark.parametrize("width", [1100, 1280, 1440, 1920])
def test_every_tier_word_has_room_before_the_business_column(browser, site, page, width):
    context = _context(browser, viewport={"width": width, "height": 900})
    tab = context.new_page()
    tab.goto(site + "#leads?tab=all")
    tab.wait_for_selector("table.leads tbody tr")
    # Every tier's word, the longest ("C possible") included.
    tab.evaluate("""() => document.querySelectorAll('table.leads tbody tr').forEach((tr, i) => {
        const [tier, word] = [['A', 'strong'], ['B', 'likely'], ['C', 'possible'], ['D', 'weak']][i % 4];
        tr.querySelector('.tier-word').textContent = ` ${tier} ${word}`; })""")
    gaps = tab.evaluate("""() => [...document.querySelectorAll('table.leads tbody tr')].map((tr) => {
        const r = document.createRange(); r.selectNodeContents(tr.querySelector('.tier-word'));
        const word = r.getBoundingClientRect(), cell = tr.querySelector('td.score').getBoundingClientRect();
        const name = tr.querySelector('td.c-name strong').getBoundingClientRect();
        return [tr.querySelector('.tier-word').textContent.trim(), r.getClientRects().length,
                Math.round(cell.right - word.right), Math.round(name.left - word.right)]; })""")
    context.close()
    assert len(gaps) >= 4
    # On one line, inside its coloured cell, with a clear gap before the business's name.
    assert all(lines == 1 and inside >= 4 and gap >= 12 for _, lines, inside, gap in gaps), gaps


def test_a_first_visit_is_offered_the_tutorial_once(browser, site, page):
    context = _context(browser, first_visit=True, viewport={"width": 1280, "height": 900})
    tab = context.new_page()
    tab.goto(site + "#find")
    offer = tab.locator(".tour-offer")
    expect(offer).to_be_visible()
    expect(offer).to_contain_text("New here?")
    tab.get_by_role("button", name="No thanks").click()
    expect(offer).to_have_count(0)
    expect(tab.locator("#tour-btn")).to_be_focused()       # where to find it later
    tab.reload()
    tab.wait_for_load_state("networkidle")
    expect(offer).to_have_count(0)                          # not offered again
    tab.click("#tour-btn")                                  # the button still opens it
    expect(tab.locator(".tour-box")).to_be_visible()
    context.close()

    # Taken instead: the tour starts, and once closed it isn't offered again either.
    context = _context(browser, first_visit=True, viewport={"width": 390, "height": 844}, has_touch=True,
                       is_mobile=True)
    tab = context.new_page()
    tab.goto(site + "#leads")
    offer = tab.locator(".tour-offer")
    expect(offer).to_be_visible()
    b = offer.bounding_box()
    assert b["x"] >= 0 and b["x"] + b["width"] <= 390 and b["y"] + b["height"] <= 844
    tab.get_by_role("button", name="Take the tour").tap()
    expect(tab.locator(".tour-box")).to_be_visible()
    expect(offer).to_have_count(0)
    tab.keyboard.press("Escape")
    tab.reload()
    tab.wait_for_load_state("networkidle")
    expect(tab.locator(".tour-offer")).to_have_count(0)
    context.close()


# ---- round 25: empty pages that can't be taken for loading, a tour offer that covers nothing,
# the minimum score in plain words, and areas never searched read "no search yet"

# Each empty page's picture: whether anything round or moving is in its box (the loading spinner
# is a turning ring), and what it is drawn with.
EMPTY_PICTURE = """(sel) => {
  const box = document.querySelector(sel);
  const pic = box.querySelector('.empty-icon'), svg = pic && pic.querySelector('svg');
  const round = [box, ...box.querySelectorAll('*')].filter((n) => {
    const s = getComputedStyle(n), r = n.getBoundingClientRect();
    return r.width >= 20 && Math.abs(r.width - r.height) < 4 &&
           parseFloat(s.borderTopLeftRadius) >= r.width / 2 - 1;
  }).map((n) => n.className.baseVal ?? n.className);
  return { kind: pic && pic.classList[1], shown: !!pic && pic.getBoundingClientRect().height > 0,
           stroke: svg && svg.getAttribute('stroke'), colour: svg && getComputedStyle(svg).color,
           tile: pic && getComputedStyle(pic).backgroundColor, round,
           moving: [box, ...box.querySelectorAll('*')].some((n) => n.getAnimations().length),
           ring: getComputedStyle(box, '::before').content };
}"""


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_empty_pages_show_a_still_picture_of_the_page_not_a_spinner(browser, site, theme):
    """Calls, Stats, Leads and the Map with nothing saved: each shows its own still picture (a phone,
    a chart, a list, a pin; drawn in the text's colour, so it suits the theme), never a ring that
    could be taken for the loading spinner."""
    context = _context(browser, viewport={"width": 1280, "height": 900}, color_scheme=theme)
    tab = context.new_page()
    seen = {}
    for page_name, sel, kind in [("calls", "#calls-wrap .empty", "phone"), ("stats", "#stats-empty .empty", "chart"),
                                 ("leads", "#leads-wrap .empty", "list"), ("map", "#map-empty .empty", "pin")]:
        tab.goto(site + "#" + page_name)
        tab.wait_for_selector(sel + " .empty-icon svg")
        seen[page_name] = tab.evaluate(EMPTY_PICTURE, sel)
        assert seen[page_name]["kind"] == kind and seen[page_name]["shown"], seen
    expect(tab.locator("#map-empty")).to_contain_text("No saved businesses yet.")
    context.close()
    for got in seen.values():
        assert got["stroke"] == "currentColor" and got["colour"] != got["tile"], seen
        assert got["round"] == [] and not got["moving"] and got["ring"] == "none", seen


# Whether the tour offer covers anything: its box against each lead row (or card), the map, the
# map's key and every search area's line; and how it is placed.
OFFER_OVER = """(sels) => {
  const offer = document.querySelector('.tour-offer'), o = offer.getBoundingClientRect();
  const over = [...document.querySelectorAll(sels)].filter((n) => n.getClientRects().length).filter((n) => {
    const r = n.getBoundingClientRect();
    return r.left < o.right && o.left < r.right && r.top < o.bottom && o.top < r.bottom;
  }).map((n) => n.id || n.className || n.tagName);
  return { over, position: getComputedStyle(offer).position, bottom: o.bottom, left: o.left, right: o.right,
           wide: document.documentElement.scrollWidth > innerWidth };
}"""


@pytest.mark.parametrize("width,height,theme", [(320, 640, "light"), (390, 844, "dark"), (768, 1024, "light"),
                                                (1440, 900, "dark"), (2560, 1440, "light")])
def test_the_tour_offer_moves_the_page_down_and_never_covers_a_lead_or_the_map(browser, site, page, width,
                                                                               height, theme):
    """The first visit's offer of the tour is a banner in the page's flow at the top of the content:
    every lead row and card, the map, its key and every search area stay fully visible beside it."""
    context = _context(browser, first_visit=True, viewport={"width": width, "height": height}, color_scheme=theme)
    tab = context.new_page()
    tab.goto(site + "#leads")
    tab.wait_for_selector("#leads-wrap .c-mark")
    offer = tab.locator(".tour-offer")
    expect(offer).to_be_visible()
    rows = tab.evaluate(OFFER_OVER, "#leads-wrap tbody tr, #page-leads .toolbar, #lead-tabs, #page-leads h1")
    assert rows["over"] == [] and rows["position"] == "static" and not rows["wide"], rows
    assert rows["left"] >= 0 and rows["right"] <= width, rows
    # Every row's middle is the row itself, not the offer.
    hidden = tab.evaluate("""() => [...document.querySelectorAll('#leads-wrap tbody tr')].filter((tr) => {
        const r = tr.getBoundingClientRect(), x = r.left + r.width / 2, y = r.top + Math.min(r.height / 2, 20);
        return y < innerHeight && !tr.contains(document.elementFromPoint(x, y)); }).length""")
    assert hidden == 0
    tab.click("nav a[data-page=map]")
    tab.wait_for_selector("#map-areas li")
    expect(offer).to_be_visible()                       # the same banner, still above the page
    tab.evaluate("scrollTo(0, 0)")
    on_map = tab.evaluate(OFFER_OVER, "#map-box, #map-groups, .map-legend, #map-areas li, #map-coverage, "
                                      "#page-map h1")
    assert on_map["over"] == [] and on_map["position"] == "static" and not on_map["wide"], on_map
    context.close()


def test_the_minimum_score_is_written_out_in_plain_words(page):
    """More options says what the minimum score means: out of 100, and that the usual one leaves
    out only tier D, the weakest businesses."""
    from leadgen.scoring import TIERS
    tier_c_floor = dict((t, low) for low, t in TIERS)["C"]
    assert tier_c_floor == config.DEFAULT_MIN_SCORE              # so the words below stay true
    page.click("nav a[data-page=find]")
    page.locator("details.advanced summary").click()
    field = page.get_by_label("Leave out weak leads: scores below (out of 100)")
    expect(field).to_have_value(str(config.DEFAULT_MIN_SCORE))
    label = page.locator("label", has=page.locator("input[name=min_score]"))
    expect(label).to_contain_text(f"The usual {config.DEFAULT_MIN_SCORE} leaves out only tier D, the weakest "
                                  "businesses. Lower keeps more of them; 0 keeps every one.")


def test_before_any_search_the_areas_read_no_search_yet_never_not_known(page):
    """With no search of AARCO's area on record, each area's name on the map and its line in the
    list say "no search yet"; nothing says "not known"."""
    page.click("nav a[data-page=map]")
    page.wait_for_selector("#map-areas li")
    expect(page.locator("#map .area-name.never small").first).to_have_text("no search yet")
    assert set(page.locator("#map .area-name small").all_text_contents()) == {"no search yet"}
    expect(page.locator("#map-areas li").first).to_contain_text("No search yet")
    expect(page.locator("#map-coverage")).to_have_text(
        "No search of AARCO's area has been recorded yet, so the areas aren't shaded.")
    assert "not known" not in page.locator("#map-areas").inner_text().lower()
    expect(page.locator("#map-empty")).to_be_hidden()      # businesses are saved: no empty note


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
    assert pages == {"find", "leads", "calls", "map", "stats", "customers"}
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


# How the Find page says how much of the area a search covered: the coverage notices shown
# (each visible notice box that speaks of it), the Details lines that do, every "N of M
# areas" figure on the page, and any words a salesperson shouldn't meet.
COVERAGE_SAID = """() => {
    const shown = (e) => Boolean(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
    const about = /of the [0-9]+ areas|whole [0-9]+-mile area|of the area|covered|filling in|didn't come in/i;
    const page = document.getElementById('page-find');
    const notices = [...page.querySelectorAll('.warn, [role=status], .h-why')]
        .filter((e) => shown(e) && about.test(e.textContent)).map((e) => e.textContent);
    const details = [...page.querySelectorAll('dl.details > div')]
        .filter((d) => shown(d) && about.test(d.textContent)).map((d) => d.textContent);
    const text = page.innerText;
    const figures = [...text.matchAll(/(\\d+) of (?:the )?(\\d+) areas/gi)].map((m) => m[1] + '/' + m[2]);
    return { notices, details, figures,
             banned: ['map areas', 'areas that answered', 'asked again automatically', 'asked again']
                 .filter((w) => text.toLowerCase().includes(w)) };
}"""


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
    note = tab.locator("#coverage-note")
    # How much is covered, which towns are still filling in and until when, that calling
    # can start now and that the rest appears on its own.
    expect(note).to_contain_text("Covered so far: about 5 of the 9 areas within 30 miles. Still filling in, "
                                 "in the background until about ")
    expect(note).to_contain_text(": Layton, West Point, Coalville and the area to the northwest. The 512 "
                                 "businesses already found are on the Leads page, ready to call now; new ones "
                                 "from the rest of the area will appear there on their own.")
    expect(tab.locator("#history tbody").first).to_contain_text("Filling in")
    assert tab.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    daily.finish(day, {"leads": 530, "new": 530, "partial": False,
                       "fill": {**fill, "state": "complete", "left": 0, "found": 18, "new": 18}})
    tab.evaluate("loadSearches()")
    expect(note).to_have_text("Covered: the whole 30-mile area. The last areas came in later, in the background "
                              "(18 more businesses, 18 new). Everything found is on the Leads page, ready to call.")
    first = tab.locator("#history tbody").first
    expect(first).to_contain_text("530")
    assert "Incomplete" not in first.inner_text() and "Filling in" not in first.inner_text()
    # Stopped by Pause searching: the parts left weren't searched (not "never came in").
    daily.finish(day, {"fill": {**fill, "state": "stopped", "why": "paused", "left": 2, "found": 18, "new": 18,
                                "where": "Coalville and the area to the north-west"}})
    tab.evaluate("loadSearches()")
    expect(note).to_have_text("Covered: about 7 of the 9 areas within 30 miles; not searched: Coalville and the "
                              "area to the northwest. Filling in stopped because searching was paused, so the rest "
                              "wasn't searched today. The 530 businesses already found are on the Leads page, ready "
                              "to call now.")
    assert "never" not in tab.inner_text("#page-find")
    context.close()


def test_a_partly_answered_search_says_its_coverage_once(page, monkeypatch):
    """A real search whose free map data answered for only part of the area (the mirrors
    refuse the northern third until they are let through): the Find page says how much
    was covered in one notice, the expanded Details in one line, while the rest fills in
    and once it is in; no figure disagrees with "N of 9", and no internal words or counts
    are shown."""
    import time

    from test_map_data import _answer, _box_of

    from leadgen import fillin
    from leadgen.http import HttpError
    from leadgen.sources import osm
    let_through = threading.Event()

    def servers(method, url, data, **kw):
        box = _box_of(data["data"])
        if box[0] > 40.8 and not let_through.is_set():
            raise HttpError(f"{url}: ConnectionResetError(104, 'Connection reset by peer')")
        return _answer(box)
    monkeypatch.setattr(osm, "request_json", servers)
    monkeypatch.setattr(fillin, "ON", True)
    monkeypatch.setattr(config, "FILL_IN_PAUSE_SECONDS", 0.2)
    monkeypatch.setattr(config, "OVERPASS_ENDPOINTS", config.OVERPASS_ENDPOINTS[:1])
    page.click("nav a[data-page=find]")
    page.wait_for_selector("text=Today's is available")
    page.click("#go")
    page.click("#confirm-go")
    page.wait_for_selector("#done-card:not([hidden])", timeout=60000)
    note = page.locator("#coverage-note")
    expect(note).to_contain_text("Covered so far: 6 of the 9 areas within 30 miles searched in full. Still "
                                 "filling in")
    expect(note).to_contain_text("ready to call now; new ones from the rest of the area will appear there on "
                                 "their own.")
    page.get_by_role("button", name="Details").first.click()
    said = page.evaluate(COVERAGE_SAID)
    assert len(said["notices"]) == 1 and said["notices"][0].startswith("Covered so far"), said
    assert len(said["details"]) == 1 and "6 of 9 areas searched in full so far; filling in" in said["details"][0], said
    assert set(said["figures"]) == {"6/9"} and not said["banned"], said

    # The rest comes in: the same one notice says so, and the Leads page has the new businesses.
    let_through.set()
    for _ in range(400):
        if not fillin.running:
            break
        time.sleep(0.05)
    assert not fillin.running
    page.evaluate("loadSearches()")
    expect(note).to_contain_text("Covered: the whole 30-mile area. The last areas came in later, in the background")
    page.get_by_role("button", name="Details").first.click()
    said = page.evaluate(COVERAGE_SAID)
    assert len(said["notices"]) == 1 and said["notices"][0].startswith("Covered: the whole"), said
    assert len(said["details"]) == 1 and said["details"][0].startswith("Free map data: area covered"), said
    assert "All 9 areas" in said["details"][0] and not said["banned"], said
    assert not [f for f in said["figures"] if f.split("/")[1] != "9"], said


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
    expect(failed).to_contain_text("Yes not saved. You're offline. Your answer is kept here; press Try again when "
                                   "you're back online.")
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


def _drop(page, url, routes):
    """Let go of requests held without an answer (the page gave up on them already), then
    stop holding: in that order, as a held request let go after page.unroute() can no
    longer be, and Playwright would keep waiting for it."""
    while routes:
        with contextlib.suppress(Exception):
            routes.pop().abort()
    page.unroute(url)


def test_a_save_that_gets_no_answer_says_so_and_can_be_tried_again(page):
    """A Yes / No or a call whose request never gets an answer (a weak connection): after
    TIMING.saveSlow the page says it is taking longer than usual, after TIMING.saveLimit it
    gives up with "not saved" and Try again (the call's notes kept), and the cancelled
    request's late answer changes nothing. Tried again, each is saved once."""
    from leadgen import calls, marks, store
    page.context.add_init_script("window.leadgenTiming = { saveSlow: 500, saveLimit: 2000 };")
    page.reload()
    page.wait_for_selector("table.leads")
    lost = []
    page.route("**/mark", lambda route: lost.append(route))           # never answered
    row = _row(page, "Costco")
    row.locator(".mark button.yes").click()
    expect(row.locator(".saving")).to_have_text("Saving Yes… This is taking longer than usual. Still trying…")
    failed = row.locator(".mark-failed")
    expect(failed).to_contain_text("Yes not saved. The Lead Finder didn't answer within 2 seconds, so it may not "
                                   "have been saved. Check your connection and try again.")
    expect(row.locator(".saving")).to_have_count(0)
    assert not row.locator(".mark button.on").count() and lost
    while lost:                                       # the late answer: the page stopped waiting for it
        with contextlib.suppress(Exception):
            lost.pop().fulfill(status=200, content_type="application/json", body='{"ok": true, "undo": null}')
    page.wait_for_timeout(300)
    assert not row.locator(".mark button.on").count() and failed.is_visible()
    page.unroute("**/mark")
    failed.get_by_role("button", name="Try again to save Yes for Costco Wholesale").click()
    expect(row).to_contain_text("Saved. Moves to “Has baler or compactor”.")
    expect(row.locator(".mark-failed")).to_have_count(0)
    expect(page.get_by_role("button", name="Has baler or compactor (1)")).to_be_visible()
    with store.connect() as db:
        assert db.one("SELECT COUNT(*) FROM mark_changes")[0] == 1

    # A Yes that reached the server after the page gave up: the next refresh clears the note.
    page.route("**/mark", lambda route: lost.append(route))
    other = _row(page, "Hampton")
    other.locator(".mark button.yes").click()
    expect(other.locator(".mark-failed")).to_contain_text("Yes not saved.")
    uid = next(lead.uid for lead in saved.load() if lead.name == "Hampton Inn")
    marks.set_mark(uid, "yes", "Tester")
    page.evaluate("loadChanges()")
    expect(other.locator(".mark-failed")).to_have_count(0)
    expect(other.locator(".mark button.yes.on")).to_have_count(1)
    _drop(page, "**/mark", lost)

    # A call: the box says it is slow, then not saved, with the notes still in it.
    page.route("**/calls", lambda route: lost.append(route))
    _row(page, "Smith").get_by_role("button", name="Just called").click()
    page.fill("#call-notes", "Spoke to Dana; send a quote.")
    page.locator("#outcomes").get_by_role("button", name="Follow Up").click()
    page.click("#call-save")
    expect(page.locator("#call-slow")).to_have_text("Saving… This is taking longer than usual. Still trying…")
    expect(page.locator("#call-error")).to_contain_text("Not saved. The Lead Finder didn't answer within 2 seconds")
    expect(page.locator("#call-error")).to_contain_text("Check your connection and try again. Your notes are kept.")
    expect(page.locator("#call-slow")).to_have_text("")
    assert page.input_value("#call-notes") == "Spoke to Dana; send a quote."
    assert page.locator("#call-save").is_enabled()
    _drop(page, "**/calls", lost)
    page.click("#call-save")
    page.wait_for_selector("#call-dlg:not([open])", state="attached")
    expect(page.locator("#toast")).to_contain_text("Smith's Marketplace: saved as Follow Up.")
    smith = next(lead.uid for lead in saved.load() if lead.name.startswith("Smith"))
    assert [c["notes"] for c in calls.history(smith)] == ["Spoke to Dana; send a quote."]


def test_a_place_outside_aarcos_area_is_named_and_needs_a_second_yes(page, monkeypatch):
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
    expect(page.locator("#confirm-far")).to_contain_text("outside AARCO's area")
    page.click("#confirm-go")                          # "Next": the second question
    far = page.locator("#far-dlg")
    expect(far).to_be_visible()
    assert "“Portland, Oregon” is 633 miles from AARCO's shop" in far.inner_text()
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
    expect(where).to_contain_text("Searched around Portland, Oregon · 633 miles from AARCO")


def test_the_place_found_is_named_before_a_search_in_the_area(page, monkeypatch):
    from leadgen import geo
    monkeypatch.setattr(geo, "request_json", lambda *a, **k: [])     # the lookups find nothing
    page.click("nav a[data-page=find]")
    page.wait_for_selector("text=Today's is available")
    page.fill("input[name=location]", "Murray")
    page.click("#go")
    expect(page.locator("#confirm-list")).to_contain_text("Murray, UT")
    assert re.search(r"From AARCO's shop\s+5\.\d miles", page.inner_text("#confirm-list"))
    assert page.locator("#confirm-far").is_hidden() and page.inner_text("#confirm-go") == "Start search"
    page.click("#confirm-back")
    page.fill("input[name=location]", "Nowhereville zz")
    page.click("#go")
    expect(page.locator("#err-location")).to_contain_text("Could not find the place")
    assert not page.locator("#confirm-dlg").is_visible()


def test_a_road_found_for_what_was_typed_is_named_and_needs_its_own_yes(page, monkeypatch):
    """"Springfield" finds only Springfield Road: the confirmation says so, with the safe
    choice in focus, and the search starts only on "Yes, search there". "1900-01-01" is
    refused at the field without being looked up. (The lookup is a stand-in.)"""
    from leadgen import geo
    from leadgen.pipeline import RunResult
    road = {"lat": "40.7801", "lon": "-111.9795", "class": "highway", "type": "residential",
            "name": "Springfield Road", "display_name": "Springfield Road, Salt Lake City, Utah",
            "address": {"road": "Springfield Road", "city": "Salt Lake City", "state": "Utah"}}
    monkeypatch.setattr(geo, "request_json", lambda *a, **k: [road])
    monkeypatch.setattr(web.finding, "run", lambda params, progress: RunResult(
        [], params.center, params.place, [], {"leads kept": 0, "seconds": 2.0}))
    sent = _posts(page, "/search")
    page.click("nav a[data-page=find]")
    page.wait_for_selector("text=Today's is available")
    page.fill("input[name=location]", "1900-01-01")
    page.click("#go")
    expect(page.locator("#err-location")).to_contain_text("Could not find the place '1900-01-01'")
    assert not page.locator("#confirm-dlg").is_visible() and not sent
    page.fill("input[name=location]", "Springfield")
    page.click("#go")
    expect(page.locator("#confirm-dlg")).to_be_visible()
    expect(page.locator("#confirm-far")).to_contain_text(
        "We found a road called Springfield Road in Salt Lake City. Is that what you meant? If not, go "
        "back and type a ZIP code, a town or a street address.")
    assert page.inner_text("#confirm-go") == "Yes, search there"
    assert page.evaluate("document.activeElement.id") == "confirm-back"   # the safe choice
    page.click("#confirm-back")
    assert not page.locator("#confirm-dlg").is_visible() and not sent
    page.click("#go")
    expect(page.locator("#confirm-dlg")).to_be_visible()
    page.click("#confirm-go")
    page.wait_for_selector("#done-card:not([hidden])")
    assert len(sent) == 1


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
    assert len(sizes) == 6                                   # the five pages and Menu
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


def test_the_line_beside_find_leads_says_a_search_is_running(page, monkeypatch):
    """While today's search runs, the line beside the greyed-out Find leads button says so
    in words (its dot never stands alone), here and after a reload."""
    from leadgen.pipeline import RunResult
    go = threading.Event()

    def run(params, progress):
        go.wait(20)
        return RunResult([], params.center, params.place, [], {"leads kept": 0, "seconds": 1})
    monkeypatch.setattr(web.finding, "run", run)
    page.click("nav a[data-page=find]")
    page.wait_for_selector("text=Today's is available")
    page.fill("input[name=location]", "Murray")
    page.click("#go")
    page.click("#confirm-go")
    page.wait_for_selector("#progress-card:not([hidden])")
    note = page.locator("#day-note")
    try:
        expect(note).to_have_text("Today's search is running; progress is below.")
        expect(note).to_be_visible()
        expect(page.locator("#go")).to_be_disabled()
        page.reload()                                  # the page picks the running search up again
        page.wait_for_selector("#progress-card:not([hidden])")
        expect(note).to_have_text("Today's search is running; progress is below.")
        expect(page.locator("#go")).to_be_disabled()
    finally:
        go.set()
    page.wait_for_selector("#done-card:not([hidden])")
    expect(note).to_contain_text("The next one can run tomorrow.")
    # With no words the line is hidden, so the dot can't show on its own.
    page.evaluate("setGo(false, '')")
    expect(note).to_be_hidden()


def test_a_pages_address_typed_in_the_address_bar_opens_that_page(page, site):
    """/leads, /calls, /map, /stats and /find typed into the address bar open the app on
    that page, never raw data or an error page."""
    for name in ("leads", "calls", "map", "stats", "find"):
        page.goto(f"{site}{name}")                     # the site's address ends in "/"
        expect(page.locator(f"#page-{name}")).to_be_visible()
        assert page.url == f"{site}#{name}", page.url
        assert not page.inner_text("body").lstrip().startswith("{")
        assert "isn't available" not in page.title() and "not found" not in page.title()


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
        expect(tab.locator("#day-note")).to_have_text(
            "The server restarted during the search; the 2 businesses it had found were saved. Today's search is "
            "still available.")
        expect(tab.locator("#go")).to_be_enabled()
        assert sorted(l.name for l in saved.load()) == ["Costco", "Smith's Marketplace"]
    finally:
        gate.set()
        context.close()
        server.shutdown()
    assert not errors


@pytest.mark.parametrize("found", [0, 3])
def test_the_find_page_and_the_history_agree_after_a_restart_that_saved_nothing(browser, site, page, found):
    """A restart before the search kept anything: the Find page says nothing was saved (as
    the history row does), never "what it had found was saved"."""
    from leadgen import daily, interrupted
    day, _ = daily.claim({"location": "Murray", "radius": 30})
    daily.release(day, interrupted.reason(0, bool(found)),
                  {"interrupted": True, "leads": 0, "new": 0,
                   "details": {"osm raw results": found} if found else {}})
    context = _context(browser, viewport={"width": 1280, "height": 900})
    tab = context.new_page()
    tab.goto(site + "#find")
    note = tab.locator("#day-note")
    history = tab.locator("#history tbody").first
    # The Find page and the history say it in the same words.
    if found:
        said = ("The server restarted during the search; none of the places it had found were within the radius "
                "and good enough to keep.")
    else:
        said = "The server restarted during the search, before it had found any businesses."
    expect(note).to_have_text(f"{said} Today's search is still available.")
    expect(history).to_contain_text(f"{said} It didn't use up the day's search.")
    assert "was saved (see" not in note.inner_text()
    expect(tab.locator("#go")).to_be_enabled()
    context.close()


def _history_with_searches():
    from leadgen import daily
    day, _ = daily.claim({"location": "876 Fortune Rd, Salt Lake City, UT 84104", "radius": 30,
                          "place": "AARCO Compactor, 876 Fortune Rd, Salt Lake City, UT 84104"})
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


# A 30-mile search around AARCO whose map data answered only partly (the numbers of a real one).
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


# The words of each visible cell the selector picks (table headings), and those split
# across two lines (a break inside a word, "Chec / ked"); a cell may wrap between words.
WORDS_SPLIT = r"""(selector) => {
    const seen = [], split = [];
    for (const th of document.querySelectorAll(selector)) {
        if (!th.offsetWidth || !th.textContent.trim()) continue;
        seen.push(th.textContent.trim());
        const text = document.createTreeWalker(th, NodeFilter.SHOW_TEXT);
        for (let node = text.nextNode(); node; node = text.nextNode()) {
            for (const word of node.data.matchAll(/\S+/g)) {
                const tops = new Set();
                for (let i = word.index; i < word.index + word[0].length; i++) {
                    const r = document.createRange(); r.setStart(node, i); r.setEnd(node, i + 1);
                    const box = r.getBoundingClientRect();
                    if (box.width) tops.add(Math.round(box.top));
                }
                if (tops.size > 1) split.push(word[0]);
            }
        }
    }
    return { seen, split }; }"""


@pytest.mark.parametrize("width", [320, 375, 390, 1100, 1440])
def test_no_table_heading_breaks_inside_a_word(browser, site, page, width):
    from leadgen import calls, marks
    lead = saved.load()[0]
    marks.set_mark(lead.uid, "yes", "Dana Smith")           # Stats shows its table once one is marked
    calls.log_call(lead.uid, "Follow Up", "Asked for the manager; " + LONG_WORD, by="Dana Smith")
    context = _context(browser, viewport={"width": width, "height": 900})
    tab = context.new_page()
    shown = {}
    for name, ready in (("stats", "#s-table tbody tr"), ("leads", "table.leads tbody tr"),
                        ("calls", "table.plain.calls tbody tr")):
        tab.goto(site + "#" + name)
        tab.wait_for_selector(ready)
        if name == "stats":
            tab.wait_for_function("document.querySelectorAll('#s-table tbody tr').length === 4")
        said = tab.evaluate(WORDS_SPLIT, "th")
        assert said["split"] == [], (name, said)
        shown[name] = said["seen"]
        if name == "stats":
            # The tier table's own words ("None checked yet") stay whole too, and all four
            # columns fit without scrolling sideways.
            assert tab.evaluate(WORDS_SPLIT, "#s-table td")["split"] == []
            assert tab.evaluate("(() => { const t = document.getElementById('s-table'); "
                                "return t.scrollWidth <= t.parentElement.clientWidth; })()")
        # Long text people typed still wraps inside its card: nothing scrolls sideways.
        assert tab.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), name
    assert shown["stats"] == ["Tier", "Checked", "Have one", "Share"]
    if width >= 1100:                         # narrower windows show cards, without headings
        assert shown["leads"] and shown["calls"]
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


# ---- the Map page

def _search_of_aarcos_area(missing):
    """The latest search of AARCO's area, as its day's record keeps it, with the map
    data's parts `missing`."""
    import json
    import time

    from leadgen import store
    info = {"location": config.OWN_ADDRESS, "radius": config.SERVICE_AREA_MILES, "source": "auto",
            "center": list(config.SERVICE_CENTER), "miles": 0.0, "leads": 4, "new": 4, "partial": bool(missing),
            "map_areas": {"missing": missing}}
    with store.connect() as db:
        db.run("INSERT INTO searches (day, at, info) VALUES (?, ?, ?)", ("2026-09-30", time.time(), json.dumps(info)))


def _on_screen(page, lat, lon):
    """Where a point of the map is in the window."""
    return page.evaluate("""([lat, lon]) => { const p = M.map.latLngToContainerPoint([lat, lon]);
        const r = document.getElementById('map').getBoundingClientRect(); return [r.left + p.x, r.top + p.y]; }""",
                         [lat, lon])


def test_the_map_shows_aarco_its_area_the_areas_and_every_saved_business(page, site):
    from leadgen import area_map, marks
    marks.set_mark(_uid("Smith"), "yes")
    marks.set_mark(_uid("Hampton"), "no")
    layton = next(a for a in area_map.areas() if a.name == "Layton")
    _search_of_aarcos_area([list(layton.box)])
    page.click("nav a[data-page=map]")
    expect(page.locator("#page-map h1")).to_have_text("Map")
    expect(page).to_have_title(re.compile(r"^Map · "))
    # AARCO's own pin, named.
    aarco = page.locator("#map .aarco-mark")
    expect(aarco).to_be_visible()
    expect(aarco.locator(".aarco-name")).to_have_text("AARCO")
    # The 30-mile circle and the nine areas, named, the one the search missed marked so.
    assert page.evaluate("M.ring !== null && M.map.hasLayer(M.ring)")
    assert page.evaluate("M.shapes.map(s => s.status)").count("missed") == 1
    names = page.locator("#map .area-name")
    expect(names).to_have_count(9)
    expect(page.locator("#map .area-name", has_text="Salt Lake City")).to_be_visible()
    expect(page.locator("#map .area-name.missed")).to_contain_text("Layton")
    expect(page.locator("#map .area-name.missed small")).to_have_text("not searched")
    expect(page.locator("#map-areas li")).to_have_count(9)
    expect(page.locator("#map-areas li", has_text="Layton")).to_contain_text("Not searched")
    expect(page.locator("#map-coverage")).to_contain_text("8 of the 9 areas searched in full")
    # Without the street map (refused here, as offline) the page says so and draws the rest.
    expect(page.locator("#map-tiles-note")).to_be_visible()
    # A pin for every saved business, counted in the key. Zoomed out, the four (about a mile
    # apart) are one circle with their count; tapping it zooms in to three dots and the
    # competitor's square.
    assert page.evaluate("M.pins.map(p => p.group).sort()") == ["competitor", "no", "unchecked", "yes"]
    for group in ("yes", "no", "unchecked", "competitor"):
        expect(page.locator(f"#map-n-{group}")).to_have_text("(1)")
    cluster = page.locator("#map .pin-cluster")
    expect(cluster).to_have_count(1)
    expect(cluster).to_have_text("4")
    assert cluster.get_attribute("title") == ("4 businesses close together (1 not checked yet, 1 No, 1 Yes, "
                                              "1 competitors or AARCO). Zoom in to see each one.")
    expect(page.locator("#map .pin-comp")).to_have_count(0)
    expect(page.locator("#map-cluster-note")).to_contain_text("Businesses close together show as one circle")
    zoomed_out = page.evaluate("M.map.getZoom()")
    cluster.click()
    expect(cluster).to_have_count(0)
    assert page.evaluate("M.map.getZoom()") > zoomed_out and page.evaluate("M.shown.size") == 4
    expect(page.locator("#map .pin-comp")).to_have_count(1)
    # Each group can be hidden and shown again, and stays as chosen after a reload.
    yes_box = page.locator('#map-groups input[data-group="yes"]')
    yes_box.uncheck()
    assert not page.evaluate("M.map.hasLayer(M.groups.yes)")
    page.locator('#map-groups input[data-group="competitor"]').uncheck()
    expect(page.locator("#map .pin-comp")).to_have_count(0)
    page.reload()
    expect(aarco).to_be_visible()
    expect(yes_box).not_to_be_checked()
    assert not page.evaluate("M.map.hasLayer(M.groups.yes)") and page.evaluate("M.map.hasLayer(M.groups.no)")
    expect(page.locator("#map .pin-comp")).to_have_count(0)
    # A hidden group isn't counted in the circles either.
    expect(page.locator("#map .pin-cluster")).to_have_text("2")
    yes_box.check()
    page.locator('#map-groups input[data-group="competitor"]').check()
    assert page.evaluate("M.map.hasLayer(M.groups.yes)")
    expect(page.locator("#map .pin-cluster")).to_have_text("4")
    page.locator("#map .pin-cluster").click()
    expect(page.locator("#map .pin-comp")).to_have_count(1)
    # The areas can be hidden too.
    page.locator("#map-show-areas").uncheck()
    expect(names).to_have_count(0)
    page.locator("#map-show-areas").check()
    expect(names).to_have_count(9)
    # A pin opens the business's details, and from there the business on the Leads page.
    smith = next(lead for lead in saved.load() if lead.name.startswith("Smith"))
    page.evaluate("([lat, lon]) => M.map.setView([lat, lon], 15, { animate: false })", [smith.lat, smith.lon])
    page.mouse.click(*_on_screen(page, smith.lat, smith.lon))
    box = page.locator("#map .pin-box")
    expect(box).to_contain_text("Smith's Marketplace")
    expect(box).to_contain_text("Has a baler or compactor: Yes")
    expect(box).to_contain_text("No calls logged yet")
    expect(box.get_by_role("link", name="(801) 555-0100")).to_be_visible()
    box.get_by_role("link", name="Open on the Leads page").click()
    expect(page.locator(".only-one")).to_contain_text("Showing one business, opened from the map.")
    expect(page.locator("table.leads tbody tr")).to_have_count(1)
    expect(_row(page, "Smith's Marketplace")).to_be_visible()
    assert f"lead={smith.uid}" in page.url
    page.get_by_role("button", name="Show the whole list").click()
    expect(page.locator(".only-one")).to_have_count(0)
    expect(page.locator("table.leads tbody tr")).to_have_count(4)
    assert "lead=" not in page.url
    # The competitor's square and AARCO's pin open their own details.
    page.click("nav a[data-page=map]")
    expect(box).to_have_count(0)                        # the map comes back with no details open
    rival = next(lead for lead in saved.load() if lead.name == "Pro Baler")
    page.evaluate("([lat, lon]) => M.map.setView([lat, lon], 13, { animate: false })", [rival.lat, rival.lon])
    page.locator("#map .pin-comp").click()
    expect(box).to_contain_text("Pro Baler")
    expect(box).to_contain_text("Competitor")
    page.evaluate("([lat, lon]) => M.map.setView([lat, lon], 13, { animate: false })", list(config.OWN_COORDS))
    aarco.click()
    expect(box).to_have_count(1)                        # the competitor's closes (it fades out)
    expect(box).to_contain_text(config.OWN_ADDRESS)


def test_the_map_takes_the_dark_theme_and_fits_a_phone(browser, site, page):
    context = _context(browser, viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True,
                       color_scheme="dark")
    phone = context.new_page()
    errors = []
    phone.on("pageerror", lambda exc: errors.append(str(exc)))
    phone.goto(site + "#map")
    expect(phone.locator("#map .aarco-mark")).to_be_visible()
    fits = phone.evaluate("""() => {
        const r = document.getElementById('map').getBoundingClientRect();
        return { page: document.documentElement.scrollWidth > window.innerWidth, left: r.left, right: r.right,
                 height: r.height, nav: [...document.querySelectorAll('nav a')].map((a) => {
                   const b = a.getBoundingClientRect(); return b.width > 0 && b.right <= window.innerWidth; }) }; }""")
    assert not fits["page"] and fits["left"] >= 0 and fits["right"] <= 390 and fits["height"] >= 300
    assert fits["nav"] == [True] * 5
    # The map's lines and pins take the dark theme's colours (the canvas is drawn in them).
    assert phone.evaluate("M.ring.options.color") == "#e6ebf2"
    phone.click("#menu-btn")
    phone.click("[data-theme-pick=light]")
    assert phone.evaluate("M.ring.options.color") == "#1d2433"
    # The zoom buttons are easy to tap.
    zoom = phone.locator("#map .leaflet-control-zoom-in").bounding_box()
    assert zoom["width"] >= 44 and zoom["height"] >= 44
    context.close()
    assert not errors


# ---- the areas' names on a phone, among hundreds of businesses

def _crowded_salt_lake(n):
    """A /map-data answer with `n` businesses, most of them in and around Salt Lake City (as a
    real search of AARCO's area finds them) and the rest spread over the area, with the
    central area partly searched, Layton not searched and the others not known."""
    import random

    from leadgen import area_map
    rng = random.Random(24)
    body = area_map.page([], None)
    groups = ["unchecked", "unchecked", "yes", "no", "competitor"]
    pins = []
    for i in range(n):
        if i % 4:                                          # three in four around downtown
            lat, lon = rng.gauss(40.72, 0.08), rng.gauss(-111.92, 0.08)
        else:
            lat, lon = rng.uniform(40.35, 41.15), rng.uniform(-112.3, -111.45)
        group = groups[i % len(groups)]
        pins.append([f"k{i}", round(lat, 5), round(lon, 5), group, f"Business {i}", "B", 60, "", "", "", "", "",
                     False, "Competitor" if group == "competitor" else ""])
    for area in body["areas"]:
        area["status"] = {"Salt Lake City": "partly", "Layton": "missed"}.get(area["name"], "unknown")
        area["status_text"] = area_map.STATUS_TEXT[area["status"]]
    body.update(pins=pins, counts={g: sum(p[3] == g for p in pins) for g in area_map.GROUPS}, now=0)
    return body


# Each area name on the map: its box, whether it is shown, whether its middle is inside its
# own area, and the boxes it must stay clear of (the circles with a count, competitors'
# squares, AARCO's pin and name, the other names shown).
AREA_NAMES = """() => {
    const box = (node) => { const r = node.getBoundingClientRect();
                            return { left: r.left, right: r.right, top: r.top, bottom: r.bottom }; };
    const map = box(document.getElementById('map'));
    const inside = (p, ring) => { let odd = false;
      for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
        const [a, b] = [ring[i], ring[j]];
        if ((a.y > p.y) !== (b.y > p.y) && p.x < (b.x - a.x) * (p.y - a.y) / (b.y - a.y) + a.x) odd = !odd;
      }
      return odd; };
    const names = [...document.querySelectorAll('#map .area-name')].map((node, i) => {
      const b = box(node), ring = M.shapes[i].shape.getLatLngs()[0].map((p) => M.map.latLngToContainerPoint(p));
      const middle = { x: (b.left + b.right) / 2 - map.left, y: (b.top + b.bottom) / 2 - map.top };
      const style = getComputedStyle(node);
      return { name: node.textContent, box: b, shown: style.visibility !== 'hidden',
               own: inside(middle, ring), size: parseFloat(style.fontSize), halo: style.textShadow !== 'none',
               z: Number(getComputedStyle(node.parentElement).zIndex) };
    });
    const marks = [...document.querySelectorAll('#map .pin-cluster, #map .pin-comp, #map .aarco-mark svg, ' +
                                                '#map .aarco-name')]
      .map((node) => ({ box: box(node), z: Number(getComputedStyle(node.closest('.leaflet-marker-icon')).zIndex),
                        cluster: node.matches('.pin-cluster') }))
      .filter((m) => m.box.right > map.left && m.box.left < map.right && m.box.bottom > map.top &&
                     m.box.top < map.bottom);
    return { map, names, marks }; }"""


def _overlap(a, b):
    return a["left"] < b["right"] and b["left"] < a["right"] and a["top"] < b["bottom"] and b["top"] < a["bottom"]


def _names_are_clear(tab):
    """Every area name shown is whole inside the map and inside its own area, drawn above the
    circles and written with a halo, and covers no circle, square, AARCO's pin or other name."""
    seen = tab.evaluate(AREA_NAMES)
    shown = [n for n in seen["names"] if n["shown"]]
    for i, name in enumerate(shown):
        b, edge = name["box"], seen["map"]
        assert edge["left"] <= b["left"] and b["right"] <= edge["right"], name
        assert edge["top"] <= b["top"] and b["bottom"] <= edge["bottom"], name
        assert name["own"] and name["size"] >= 12 and name["halo"], name
        for mark in seen["marks"]:
            assert not _overlap(b, mark["box"]), (name, mark)
            if mark["cluster"]:
                assert name["z"] > mark["z"], (name, mark)
        for other in shown[i + 1:]:
            assert not _overlap(b, other["box"]), (name, other)
    return seen


@pytest.mark.parametrize("width", [320, 375])
def test_the_areas_names_stay_readable_among_hundreds_of_businesses_on_a_phone(browser, site, width):
    """On a phone, with several hundred businesses around Salt Lake City, no area name is hidden
    under a circle with a count: each name and its status shown on the map sits in free space
    inside its own area (moved there when circles cover its own spot), and one with no free
    space at this zoom is left to the list under the map, which a note under the map points to.
    Zooming in to Salt Lake City, its name and "partly searched" come into view."""
    import json

    body = _crowded_salt_lake(490)
    context = _context(browser, viewport={"width": width, "height": 812}, has_touch=True, is_mobile=True)
    context.route("**/map-data", lambda route: route.fulfill(status=200, content_type="application/json",
                                                             body=json.dumps(body)))
    phone = context.new_page()
    errors = []
    phone.on("pageerror", lambda exc: errors.append(str(exc)))
    phone.goto(site + "#map")
    phone.wait_for_function("M.loaded === true")
    assert phone.evaluate("M.clustered.size") >= 10               # the circles are there to collide with
    note = phone.locator("#map-names-note")
    seen = _names_are_clear(phone)
    shown = [n["name"] for n in seen["names"] if n["shown"]]
    assert shown and len(shown) < 9, shown
    expect(note).to_be_visible()
    expect(note).to_contain_text("listed under Search areas below")
    # Every area, shown on the map or not, is named with its status in the list under the map.
    expect(phone.locator("#map-areas li")).to_have_count(9)
    expect(phone.locator("#map-areas li", has_text="Salt Lake City")).to_contain_text("Partly searched")
    expect(phone.locator("#map-areas li", has_text="Layton")).to_contain_text("Not searched")
    # Zooming in step by step towards Salt Lake City, the names are placed again among the new
    # circles each time, and the home area's name and status come into view.
    home = next(a for a in body["areas"] if a["name"] == "Salt Lake City")
    found = False
    for _ in range(5):
        phone.evaluate("(at) => M.map.setView(at, M.map.getZoom() + 1, { animate: false })", home["label"])
        seen = _names_are_clear(phone)
        if any(n["shown"] and n["name"] == "Salt Lake Citypartly searched" for n in seen["names"]):
            found = True
            break
    assert found
    # Moved, they are placed again.
    phone.evaluate("M.map.panBy([60, -40], { animate: false })")
    _names_are_clear(phone)
    # With the areas switched off there are no names, and no note about them.
    phone.locator("#map-show-areas").uncheck()
    expect(phone.locator("#map .area-name")).to_have_count(0)
    expect(note).to_be_hidden()
    context.close()
    assert not errors


@pytest.mark.parametrize("width", [390, 1440])
def test_a_slow_first_load_says_the_site_is_waking_up(browser, site, page, width):
    """The site sleeps after a quiet spell and its first answer then takes a while: the page
    says it is loading, then that it is starting up and the leads appear by themselves,
    then to reload if nothing comes; the notice goes once the leads are there."""
    context = _context(browser, viewport={"width": width, "height": 800})
    context.add_init_script("window.leadgenTiming = { waking: 300, stillWaking: 900 };")
    tab = context.new_page()
    held = []
    tab.route("**/leads?*", lambda route: held.append(route))
    tab.goto(site + "#leads")
    starting = tab.locator("#starting")
    expect(starting).to_contain_text("Starting up… The Lead Finder sleeps when nobody has used it for a while")
    expect(starting).to_contain_text("Still starting. If nothing appears within two minutes, reload the page.")
    assert tab.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    for route in held:
        route.continue_()
    tab.unroute("**/leads?*")
    tab.wait_for_selector("table.leads")
    expect(starting).to_be_hidden()
    context.close()


def test_a_quick_first_load_shows_no_starting_notice(page):
    expect(page.locator("#starting")).to_be_hidden()


# ---- a pin's details on a phone

LONG_NAME = "Great Basin Regional Distribution and Fulfillment Center for Refrigerated and Frozen Grocery Goods 2"

# Where a pin's details box, its close button and the name in it are once the map has stopped
# moving to show it, and whether each fits inside the map and the screen.
PIN_BOX_FITS = """() => {
    const rect = (s) => document.querySelector(s).getBoundingClientRect();
    const map = rect('#map'), view = { left: 0, top: 0, right: window.innerWidth, bottom: window.innerHeight };
    const inside = (r, o) => r.left >= o.left && r.right <= o.right && r.top >= o.top && r.bottom <= o.bottom;
    const box = rect('#map .leaflet-popup-content-wrapper'), close = rect('#map .leaflet-popup-close-button');
    const name = document.querySelector('#map .pin-box strong');
    const text = document.querySelector('#map .leaflet-popup-content'), n = name.getBoundingClientRect();
    return { box: inside(box, map) && inside(box, view), close: inside(close, map) && inside(close, view),
             closeSize: [close.width, close.height], nameInBox: inside(n, box),
             clearOfClose: n.right <= close.left || n.top >= close.bottom,
             wrapped: n.height > 2 * parseFloat(getComputedStyle(name).lineHeight),
             sideways: text.scrollWidth > text.clientWidth,
             page: document.documentElement.scrollWidth > window.innerWidth }; }"""


@pytest.mark.parametrize("width", [360, 375, 390])
def test_a_pins_details_fit_a_phones_map_and_close_with_their_button(browser, site, page, width):
    """On a phone the map is narrower than a details box's usual width: a business with a
    100-character name, its pin near the map's right edge, opens a box that fits inside the
    map and the screen, its name wrapped whole and its close button in view, which closes it."""
    assert len(LONG_NAME) == 100
    lat, lon = config.SERVICE_CENTER
    far = Lead(name=LONG_NAME, lat=lat, lon=lon + 0.55, source="yelp", source_id="y-long", phone="(801) 555-0199",
               city="Salt Lake City", raw_categories=["yelp:grocery"], yelp_reviews=200, address="1 Long Name Way")
    score_lead(far, config.DEFAULT_KEYWORDS)
    saved.save_search([far], config.DEFAULT_KEYWORDS)
    context = _context(browser, viewport={"width": width, "height": 800}, has_touch=True, is_mobile=True)
    phone = context.new_page()
    phone.goto(site + "#map")
    expect(phone.locator("#map .aarco-mark")).to_be_visible()
    x, y = _on_screen(phone, far.lat, far.lon)
    edge = phone.locator("#map").bounding_box()
    assert edge["x"] + edge["width"] - 40 < x < edge["x"] + edge["width"]          # near the right edge
    phone.touchscreen.tap(x, y)
    box = phone.locator("#map .pin-box")
    expect(box.locator("strong").first).to_have_text(LONG_NAME)
    # The map moves to show the box: measured once it has stopped.
    phone.wait_for_function("""() => { const b = document.querySelector('#map .leaflet-popup');
        if (!b) return false; const r = JSON.stringify(b.getBoundingClientRect());
        const same = window.lastBox === r; window.lastBox = r; return same; }""", polling=200)
    fits = phone.evaluate(PIN_BOX_FITS)
    assert fits["box"] and fits["close"] and fits["nameInBox"] and fits["clearOfClose"], fits
    assert fits["wrapped"] and not fits["sideways"] and not fits["page"], fits
    assert min(fits["closeSize"]) >= 44, fits
    phone.locator("#map .leaflet-popup-close-button").tap()
    expect(box).to_have_count(0)
    context.close()


# ---- the phone bar with large counts

@pytest.mark.parametrize("width", [320, 375])
def test_the_phone_bar_keeps_one_row_whatever_the_counts(browser, site, page, width):
    """The five page links stay on one row from 320 px up with four- and five-digit counts
    beside Leads and Calls, each still a 44 x 44 px touch target."""
    context = _context(browser, viewport={"width": width, "height": 800}, has_touch=True, is_mobile=True)
    phone = context.new_page()
    phone.goto(site + "#leads")
    phone.wait_for_selector("#n-leads:not([hidden])")
    for leads, called in [(1060, 1000), (10000, 1060), (99999, 99999)]:
        phone.evaluate(f"S.counts.unchecked = {leads}; S.counts.called = {called}; counts()")
        expect(phone.locator("#n-leads")).to_contain_text(f"{leads:,}")
        expect(phone.locator("#n-calls")).to_contain_text(f"{called:,}")
        links = phone.evaluate("""() => [...document.querySelectorAll('nav a')].map((a) => {
            const r = a.getBoundingClientRect(); return [Math.round(r.top), r.width, r.height, r.left, r.right]; })""")
        assert len(links) == 5 and len({top for top, *_ in links}) == 1, (leads, called, links)
        assert all(w >= 44 and h >= 44 and left >= 0 and right <= width for _, w, h, left, right in links), links
        assert phone.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    context.close()


# ---- a page that couldn't load tries again by itself

FAILED = {"error": "The saved list can't be read right now. Try again in a minute."}


@pytest.mark.parametrize("where,path,shown", [("leads", "**/leads?*", "table.leads"),
                                              ("calls", "**/leads?*tab=called*", "#calls-body"),
                                              ("map", "**/map-data", "#map .aarco-mark"),
                                              ("stats", "**/stats", "#stats-body")])
def test_a_page_that_could_not_load_shows_its_data_once_the_site_is_back(browser, site, page, where, path, shown):
    """The site's database was down: the page says it can't show its data, with Retry, and
    without a click tries again by itself, showing the data as soon as a load works."""
    context = _context(browser, viewport={"width": 1280, "height": 800})
    context.add_init_script("window.leadgenTiming = { retry: 600 };")
    tab = context.new_page()
    failed = []

    def once(route):
        if failed:
            route.continue_()
        else:
            failed.append(route.request.url)
            route.fulfill(status=503, json=FAILED)
    tab.route(path, once)
    tab.goto(site + f"#{where}")
    problem = tab.locator(f"#{where}-problem .problem")
    expect(problem).to_contain_text("right now")
    expect(problem.get_by_role("button", name="Retry")).to_be_visible()
    # Nothing is clicked: the next quiet try works and the data replaces the box.
    expect(tab.locator(shown)).to_be_visible(timeout=10000)
    expect(problem).to_have_count(0)
    assert len(failed) == 1
    context.close()


def test_the_quiet_retries_stop_when_another_page_is_opened(browser, site, page):
    context = _context(browser, viewport={"width": 1280, "height": 800})
    context.add_init_script("window.leadgenTiming = { retry: 300 };")
    tab = context.new_page()
    asked = []

    def down(route):
        asked.append(route.request.url)
        route.fulfill(status=503, json=FAILED)
    tab.route("**/stats", down)
    tab.goto(site + "#stats")
    expect(tab.locator("#stats-problem .problem")).to_contain_text("Can't show the stats right now")
    for _ in range(50):
        if len(asked) >= 3:
            break
        tab.wait_for_timeout(100)
    assert len(asked) >= 3                             # it keeps trying while the page is open
    tab.click("nav a[data-page=find]")
    tab.wait_for_timeout(400)
    before = len(asked)
    tab.wait_for_timeout(1500)                         # five retry periods on another page
    assert len(asked) == before
    tab.unroute("**/stats")
    tab.click("nav a[data-page=stats]")                # back on the page: it loads (and works now)
    expect(tab.locator("#stats-body")).to_be_visible()
    expect(tab.locator("#stats-problem .problem")).to_have_count(0)
    context.close()


# ---- review round 24: offline or the site down, buttons named by business, separators

NOT_ANSWERING = "The Lead Finder isn't answering right now. Try again in a minute."


def test_a_stopped_site_is_named_not_the_persons_internet(browser, page):
    """The site stops while the page is open and the device is online: a Yes says the Lead
    Finder isn't answering (never "check your internet"), and so do the Leads, Calls, Map and
    Stats boxes, each with Retry."""
    from werkzeug.serving import make_server
    # One request per connection (not threaded: no keep-alive), so once it is closed nothing answers.
    server = make_server("127.0.0.1", 0, web.create_app(), threaded=False)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    context = _context(browser, viewport={"width": 1280, "height": 900})
    tab = context.new_page()
    try:
        tab.goto(f"http://127.0.0.1:{server.server_port}/#map")
        tab.wait_for_selector("#map .aarco-mark")                   # the map's library is loaded
        tab.click("nav a[data-page=leads]")
        tab.wait_for_selector("table.leads")
    finally:
        server.shutdown()
        server.server_close()
    assert tab.evaluate("navigator.onLine")
    row = _row(tab, "Costco")
    row.locator(".mark button.yes").click()
    failed = row.locator(".mark-failed")
    expect(failed).to_have_text(re.compile(r"^Yes not saved\. " + re.escape(NOT_ANSWERING)))
    expect(failed.get_by_role("button", name="Try again to save Yes for Costco Wholesale")).to_be_visible()
    for where, title in [("calls", "Can't show your calls right now"), ("map", "Can't show the map right now"),
                         ("stats", "Can't show the stats right now"), ("leads", "")]:
        tab.click(f"nav a[data-page={where}]")
        if where == "leads":
            tab.evaluate("loadLeads()")                             # its rows were shown: they stay
            expect(tab.locator("#leads-note")).to_have_text(f"Couldn't show this view. {NOT_ANSWERING}")
            continue
        box = tab.locator(f"#{where}-problem .problem")
        expect(box).to_contain_text(f"{title}{NOT_ANSWERING}")
        expect(box.get_by_role("button", name="Retry")).to_be_visible()
    assert "internet" not in tab.inner_text("body") and "offline" not in tab.inner_text("body")
    context.close()


def test_offline_the_page_says_so_and_loads_again_once_back_online(browser, site, page):
    """The device goes offline (the site is fine): a Yes says the answer is kept and to press
    Try again once back online; a page that couldn't load says it is offline, and loads by
    itself the moment the device is back online (not at its next quiet try)."""
    page.context.set_offline(True)
    assert page.evaluate("navigator.onLine") is False
    row = _row(page, "Costco")
    row.locator(".mark button.yes").click()
    expect(row.locator(".mark-failed")).to_contain_text(
        "Yes not saved. You're offline. Your answer is kept here; press Try again when you're back online.")
    page.click("nav a[data-page=stats]")
    box = page.locator("#stats-problem .problem")
    expect(box).to_contain_text("Can't show the stats right now"
                                "You're offline. Try again when you're back online.")
    assert "isn't answering" not in page.inner_text("body")
    page.context.set_offline(False)                    # the quiet try is 20 seconds away
    expect(page.locator("#stats-body")).to_be_visible(timeout=5000)
    expect(box).to_have_count(0)
    page.click("nav a[data-page=leads]")
    row.locator(".mark-failed").get_by_role("button", name="Try again to save Yes for Costco Wholesale").click()
    expect(row).to_contain_text("Saved. Moves to “Has baler or compactor”.")


def test_just_called_and_history_say_which_business_they_are_for(page):
    """Each "Just called" and "History" button, on Leads and on Calls, is named with its
    business for a screen reader or voice control; the words shown stay as they were."""
    from leadgen import calls
    calls.log_call(_uid("Costco"), "Follow Up", "Ask about the baler.")
    page.reload()
    page.wait_for_selector("table.leads")
    row = _row(page, "Costco")
    called = row.get_by_role("button", name="Just called: Costco Wholesale", exact=True)
    history = row.get_by_role("button", name="History (1 call): Costco Wholesale", exact=True)
    expect(called).to_have_text("Just called")
    expect(history).to_have_text("History (1)")
    # Tabbing on from the row's No reaches them by name.
    row.locator(".mark button.no").focus()
    names = []
    for _ in range(4):
        page.keyboard.press("Tab")
        names.append(page.evaluate("document.activeElement.getAttribute('aria-label') || "
                                   "document.activeElement.textContent"))
    assert "Just called: Costco Wholesale" in names and "History (1 call): Costco Wholesale" in names, names
    expect(page.locator("*:focus")).not_to_have_accessible_name("Just called")
    expect(_row(page, "Smith").get_by_role("button", name="Just called: Smith's Marketplace",
                                           exact=True)).to_have_text("Just called")
    page.click("nav a[data-page=calls]")
    page.wait_for_selector("table.calls")
    cell = page.locator("#calls-wrap tr", has_text="Costco Wholesale")
    expect(cell.get_by_role("button", name="Just called: Costco Wholesale", exact=True)).to_have_text("Just called")
    expect(cell.get_by_role("button", name="History (1 call): Costco Wholesale", exact=True)).to_have_text("History")


# Every "·" in the boxes `selector` picks that starts a line (nothing before it on its line).
DOT_STARTS_LINE = """(selector) => {
  const bad = [];
  let wrapped = 0;
  for (const box of document.querySelectorAll(selector)) {
    const walker = document.createTreeWalker(box, NodeFilter.SHOW_TEXT);
    const chars = [];
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      for (let i = 0; i < n.data.length; i++) if (n.data[i].trim()) chars.push([n, i]);
    }
    const rect = ([n, i]) => { const r = document.createRange(); r.setStart(n, i); r.setEnd(n, i + 1);
                               return r.getClientRects()[0]; };
    const tops = new Set(chars.map(rect).filter(Boolean).map((r) => Math.round(r.top)));
    if (tops.size > 1) wrapped++;
    chars.forEach((c, k) => {
      if (c[0].data[c[1]] !== "·") return;
      const me = rect(c), before = k ? rect(chars[k - 1]) : null;
      if (me && (!before || before.bottom <= me.top + 1)) bad.push(box.innerText);
    });
  }
  return { bad, wrapped };
}"""


@pytest.mark.parametrize("width", [1440, 1100])
def test_a_separator_never_starts_a_line(browser, site, page, width):
    """A verified phone and contact name too long for one line wrap after the "·", never
    before it: on Leads (and in each address line) and on Calls."""
    from leadgen import calls, contacts
    uid = _uid("Costco")
    contacts.save(uid, "801-555-0199", "Jane Doe, facilities manager at the north warehouse dock", "Tester")
    calls.log_call(uid, "Follow Up", "Ask about the baler.")
    context = _context(browser, viewport={"width": width, "height": 900})
    tab = context.new_page()
    tab.goto(site + "#leads")
    tab.wait_for_selector("table.leads")
    expect(_row(tab, "Costco").locator(".verified")).to_contain_text("Verified (801) 555-0199 · Ask for Jane Doe")
    said = tab.evaluate(DOT_STARTS_LINE, "table.leads .v-line, table.leads td.contact > div:first-child")
    assert not said["bad"] and said["wrapped"], said
    tab.click("nav a[data-page=calls]")
    tab.wait_for_selector("table.calls")
    said = tab.evaluate(DOT_STARTS_LINE, "table.calls .v-line, table.calls td.c-name .sub")
    assert not said["bad"] and said["wrapped"], said
    context.close()


@contextlib.contextmanager
def _logged_in_site():
    """A site behind a login, served on this machine (the page fixture's leads are its leads)."""
    from werkzeug.serving import make_server
    server = make_server("127.0.0.1", 0, web.create_app(password="pw", username="Matt"), threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()


def _log_in(tab, password="pw"):
    tab.fill("input[name=username]", "Matt")
    tab.fill("input[name=password]", password)
    tab.locator("form button[type=submit]").click()


def test_a_login_that_ended_says_what_was_not_saved_and_comes_back_to_the_same_view(browser, page):
    """The login ends (its cookie is gone) and the next click can't be saved: the login page
    says which click wasn't saved, and logging in again opens the same page and tab. The
    notes typed for a call are still there."""
    from leadgen import marks
    with _logged_in_site() as url:
        context = _context(browser, viewport={"width": 1280, "height": 900})
        tab = context.new_page()
        tab.goto(url + "login")
        _log_in(tab)
        tab.goto(url + "#leads?tab=all&q=o")
        tab.wait_for_selector("table.leads")
        view = tab.url
        # A call whose Save meets an ended login: its notes stay as the business's draft.
        _row(tab, "Smith").get_by_role("button", name="Just called").click()
        tab.fill("#call-notes", "Spoke to Dana; call back Friday.")
        tab.locator("#outcomes").get_by_role("button", name="Follow Up").click()
        context.clear_cookies()
        tab.click("#call-save")
        tab.wait_for_url("**/login?**")
        expect(tab.locator("#ended")).to_have_text(
            "Your login ended. Your call to Smith's Marketplace wasn't saved. Your notes are kept: open Just "
            "called on it again to save them. Log in to go back to where you were.")
        _log_in(tab)
        tab.wait_for_selector("table.leads")
        assert tab.url == view                                  # the same page, tab and filter
        expect(tab.locator("#lead-tabs button.on")).to_contain_text("All")
        _row(tab, "Smith").get_by_role("button", name="Just called").click()
        assert tab.input_value("#call-notes") == "Spoke to Dana; call back Friday."
        tab.click("#call-cancel")
        # A Yes that meets an ended login names the business, and is indeed not saved.
        context.clear_cookies()
        _row(tab, "Costco").locator(".mark button.yes").click()
        tab.wait_for_url("**/login?**")
        expect(tab.locator("#ended")).to_contain_text("Your Yes for Costco Wholesale wasn't saved.")
        # A wrong password keeps the note and the way back.
        _log_in(tab, "wrong")
        expect(tab.locator(".error")).to_have_text("Wrong username or password.")
        expect(tab.locator("#ended")).to_contain_text("Your Yes for Costco Wholesale wasn't saved.")
        _log_in(tab)
        tab.wait_for_selector("table.leads")
        assert tab.url == view
        expect(_row(tab, "Costco").locator(".mark button.yes")).to_have_attribute("aria-pressed", "false")
        assert not any(marks.get_all([l.uid for l in saved.load()]).values())
        # Back in, the note is gone: an ordinary visit to the login page says nothing about it.
        context.clear_cookies()
        tab.goto(url + "login")
        expect(tab.locator("#ended")).to_have_count(0)
        context.close()


def test_aarcos_own_listings_have_no_score_and_stats_names_them_apart(page):
    own = Lead(name="AARCO Compactor", lat=40.74, lon=-111.94, source="osm", source_id="own",
               raw_categories=["building=industrial"])
    score_lead(own, config.DEFAULT_KEYWORDS)
    assert own.lead_type == "Own company" and own.tier           # scored inside, never shown
    saved.save_search([own], config.DEFAULT_KEYWORDS)
    page.reload()
    page.wait_for_selector("table.leads")
    page.get_by_role("button", name="Competitors & AARCO (2)").click()
    row = _row(page, "AARCO Compactor")
    expect(row.locator("td.score")).to_have_text("Not scored")
    expect(row.locator(".tier-word")).to_have_count(0)
    expect(row.locator(".pts")).to_have_count(0)                  # no points that add up to a score
    expect(row).to_contain_text("AARCO's own listing")
    # A competitor keeps its flag and its score: it is listed, never dropped.
    expect(_row(page, "Pro Baler").locator(".tier-word")).to_have_count(1)
    expect(_row(page, "Pro Baler")).to_contain_text("Competitor: not a prospect")
    # A tier picks prospects and competitors; AARCO's own has none.
    page.select_option("#tier", own.tier)
    expect(_row(page, "AARCO Compactor")).to_have_count(0)
    page.click("nav a[data-page=stats]")
    expect(page.locator("#stats-left-out")).to_have_text(
        "1 competitor and 1 AARCO listing are left out of these numbers: they are flagged in the list, but they "
        "aren't prospects.")


def test_yes_and_no_are_named_by_the_word_they_show(page):
    yes = page.get_by_role("button", name="Yes: Costco Wholesale has a baler or compactor")
    no = page.get_by_role("button", name="No: Costco Wholesale doesn't have a baler or compactor")
    expect(yes).to_have_text("Yes")
    expect(no).to_have_text("No")
    for button in page.locator(".mark button[data-ctl=yes], .mark button[data-ctl=no]").all():
        assert button.get_attribute("aria-label").startswith(button.inner_text() + ": ")


def test_changing_a_colleagues_answer_says_whose_it_is_and_asks_first(page):
    from leadgen import marks
    sent = _posts(page, "/mark")
    _row(page, "Costco").locator(".mark button.no").click()       # marked No by Tester
    page.wait_for_selector("#recent:not([hidden])")
    page.get_by_role("button", name="All (4)").click()
    # The same person changes their own answer: no question.
    _row(page, "Costco").locator(".mark button.yes").click()
    expect(_row(page, "Costco").locator(".mark button.yes")).to_have_attribute("aria-pressed", "true")
    assert not page.locator("#mark-dlg").is_visible() and len(sent) == 2
    # Someone else (the name in the side bar changed) is asked, with who and when.
    page.click("#me-change")
    page.fill("#name-input", "Sam")
    page.click("#name-save")
    _row(page, "Costco").locator(".mark button.no").click()
    expect(page.locator("#mark-dlg")).to_be_visible()
    expect(page.locator("#mark-who")).to_have_text(re.compile(r"^Marked Yes by Tester on \w{3} \d+, \d{4}, "
                                                             r"\d+:\d\d [AP]M\.$"))
    expect(page.locator("#mark-ask")).to_contain_text("Change Costco Wholesale to No?")
    assert page.evaluate("document.activeElement.id") == "mark-cancel"
    page.click("#mark-cancel")                                     # Cancel: Tester's Yes stays
    expect(page.locator("#mark-dlg")).to_be_hidden()
    assert len(sent) == 2
    lead = next(l for l in marks.apply(saved.load()) if l.name == "Costco Wholesale")
    assert lead.has_baler == "yes" and lead.marked_by == "Tester"
    expect(_row(page, "Costco").locator(".mark button.yes")).to_have_attribute("aria-pressed", "true")
    _row(page, "Costco").locator(".mark button.no").click()
    expect(page.locator("#mark-dlg")).to_be_visible()
    page.keyboard.press("Escape")                                  # Escape: the same
    expect(page.locator("#mark-dlg")).to_be_hidden()
    assert len(sent) == 2
    _row(page, "Costco").locator(".mark button.no").click()
    page.click("#mark-go")
    expect(_row(page, "Costco")).to_contain_text("Marked by Sam")
    assert next(l for l in marks.apply(saved.load()) if l.name == "Costco Wholesale").has_baler == "no"
    # A colleague who answers after the page last looked is asked about all the same: nothing
    # is saved, and the page shows their answer and asks.
    smith = next(l for l in saved.load() if l.name.startswith("Smith"))
    marks.set_mark(smith.uid, "no", "Dana")
    _row(page, "Smith").locator(".mark button.yes").click()
    expect(page.locator("#mark-dlg")).to_be_visible()
    expect(page.locator("#mark-who")).to_contain_text("Marked No by Dana on ")
    expect(_row(page, "Smith").locator(".mark button.no")).to_have_attribute("aria-pressed", "true")
    assert marks.get_all([smith.uid]) == {smith.uid: "no"}
    page.click("#mark-go")
    expect(_row(page, "Smith")).to_contain_text("Marked by Sam")
    assert marks.get_all([smith.uid]) == {smith.uid: "yes"}


def test_the_contact_column_sorts_by_contact(page):
    """The Contact header sorts by what it shows: the businesses that can be phoned first,
    a number the team verified before the listing's own."""
    from leadgen import contacts
    hampton = next(l for l in saved.load() if l.name == "Hampton Inn")
    contacts.save(hampton.uid, "801-555-0199", "Lee", "Sam")
    page.reload()
    page.wait_for_selector("table.leads")
    assert "Hampton Inn" not in page.locator("table.leads tbody tr").first.inner_text()
    header = page.locator("table.leads thead button.sort", has_text="Contact")
    assert "contact" in header.get_attribute("title").lower()
    header.click()
    expect(page.locator("table.leads tbody tr").first).to_contain_text("Hampton Inn")
    assert "sort=contact" in page.url
    expect(page.locator("table.leads thead th", has_text="Contact")).to_have_attribute("aria-sort", "ascending")


def test_a_tier_alone_that_matches_nothing_gives_no_spelling_advice(page):
    shown = {l.tier for l in saved.load() if l.lead_type == "Prospect"}
    empty_tier = next(t for t in "ABCD" if t not in shown)
    page.select_option("#tier", empty_tier)
    box = page.locator("#leads-wrap .empty")
    expect(box).to_contain_text(f"No leads match tier {empty_tier}")
    assert "spelling" not in box.inner_text()
    expect(box).to_contain_text("Pick All tiers or another tier, pick another tab, or clear the filters")
    page.check("#has-phone")
    expect(box).to_contain_text("Pick All tiers or another tier, turn off Has phone, pick another tab")
    assert "spelling" not in box.inner_text()
    page.select_option("#tier", "")
    page.fill("#filter", "zzzz")
    expect(box).to_contain_text("Check the spelling")


# ---- a server restart while the page follows a search

@contextlib.contextmanager
def _a_search_on_its_own_server(browser, monkeypatch):
    """A site of its own whose search reports two businesses and then waits (the server is
    about to stop): yields (the tab following the search, the app)."""
    from werkzeug.serving import make_server

    from leadgen.sources import report_found
    found = [Lead(name=name, lat=40.66 + i * 0.01, lon=-111.89, source="osm", source_id=f"way/{i}",
                  raw_categories=[tag])
             for i, (name, tag) in enumerate([("Costco", "shop=wholesale"),
                                              ("Smith's Marketplace", "shop=supermarket")])]
    reported, gate = threading.Event(), threading.Event()

    def run(params, progress):
        report_found(list(found))
        reported.set()
        gate.wait(60)
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
        expect(tab.locator("#progress-msg")).not_to_have_text("")
        yield tab, app
    finally:
        gate.set()
        context.close()
        server.shutdown()
    assert not errors


def test_the_progress_card_says_it_is_reconnecting_and_its_timer_keeps_counting(browser, monkeypatch):
    """The server stops answering mid-search: within 3 seconds of the status check that got no
    answer the card says "Reconnecting…" and its timer goes on; when the server is back (it
    restarted) every place says so in one sentence, and Find leads is available again."""
    import time

    from leadgen import interrupted
    with _a_search_on_its_own_server(browser, monkeypatch) as (tab, app):
        asked = []
        tab.route("**/status/*", lambda route: asked.append((time.monotonic(), route)))   # no answer
        msg = tab.locator("#progress-msg")
        expect(msg).to_have_text("Reconnecting…", timeout=6000)
        seen = time.monotonic()
        assert asked and seen - asked[0][0] < 3.0, seen - asked[0][0]
        expect(tab.locator("#slow-note")).to_contain_text("The server isn't answering just now. This page keeps "
                                                          "trying")
        before = tab.inner_text("#elapsed")
        expect(tab.locator("#elapsed")).not_to_have_text(before, timeout=3000)       # the timer keeps counting
        expect(tab.locator("#go")).to_be_disabled()
        # The restart: the old process says its search ended; the new one has no jobs.
        interrupted._shutting_down()
        interrupted._live.clear()
        app.extensions["leadgen"].jobs.clear()
        tab.unroute("**/status/*")
        for _, route in asked:
            with contextlib.suppress(Exception):
                route.abort()
        said = "The server restarted during the search; the 2 businesses it had found were saved."
        expect(tab.locator("#day-note")).to_have_text(f"{said} Today's search is still available.", timeout=20000)
        expect(tab.locator("#progress-card")).to_be_hidden()
        expect(tab.locator("#history tbody").first).to_contain_text(f"{said} It didn't use up the day's search.")
        expect(tab.locator("#go")).to_be_enabled()
        assert "site was updated" not in tab.inner_text("#page-find")


def test_after_a_crash_find_leads_says_when_it_is_back_and_comes_back_by_itself(browser, monkeypatch):
    """A killed server says no goodbye: its search is finished once its last sign of life is
    old enough. Until then the page says what it is doing and when Find leads is available
    again, and turns it back on by itself."""
    from leadgen import interrupted, store
    with _a_search_on_its_own_server(browser, monkeypatch) as (tab, app):
        [run] = interrupted._live.values()
        run.write()                      # its last heartbeat ...
        run._stop.set()                  # ... and the process is killed
        interrupted._live.clear()
        app.extensions["leadgen"].jobs.clear()
        with store.connect() as db:      # 15 seconds ago: about 30 seconds to go
            db.run("UPDATE search_runs SET alive = alive - 15")
        note = tab.locator("#day-note")
        expect(note).to_contain_text("The server restarted during the search; what it had found is being saved. "
                                     "Find leads is available again by ", timeout=20000)
        expect(note).to_contain_text("(in about 30 seconds); this page updates by itself.")
        assert re.search(r"available again by \d{1,2}:\d\d [AP]M \(", note.inner_text())
        expect(tab.locator("#go")).to_be_disabled()
        expect(tab.locator("#history tbody").first).to_contain_text(
            "The server restarted during the search; what it had found is being saved.")
        # Time passes: the page asks again by itself and Find leads comes back.
        with store.connect() as db:
            db.run("UPDATE search_runs SET alive = alive - ?", (interrupted.STALE_SECONDS,))
        expect(tab.locator("#go")).to_be_enabled(timeout=12000)
        expect(note).to_have_text("The server restarted during the search; the 2 businesses it had found were "
                                  "saved. Today's search is still available.")


# ---- the Map page with many businesses

def _many_pins(n):
    """A /map-data answer with `n` synthetic businesses spread over AARCO's area (and two at
    one address), each group in turn."""
    import math
    import random

    from leadgen import area_map
    rng = random.Random(14)
    body = area_map.page([], None)
    lat0, lon0 = config.SERVICE_CENTER
    groups = ["unchecked", "unchecked", "yes", "no", "competitor"]
    pins = []
    for i in range(n - 2):
        r, a = 25 * math.sqrt(rng.random()), rng.random() * 2 * math.pi
        lat = lat0 + r * math.cos(a) / 69.05
        lon = lon0 + r * math.sin(a) / (69.05 * math.cos(math.radians(lat0)))
        group = groups[i % len(groups)]
        pins.append([f"k{i}", round(lat, 5), round(lon, 5), group, f"Business {i}", "B", 60, "(801) 555-0100", "",
                     f"{i} Main St", "", "", False, "Competitor" if group == "competitor" else ""])
    for i, name in enumerate(["Twin Suite A", "Twin Suite B"]):
        pins.append([f"twin{i}", 40.9, -112.15, "yes", name, "A", 80, "", "", "1 Twin Rd", "", "", False, ""])
    body.update(pins=pins, counts={g: sum(p[3] == g for p in pins) for g in area_map.GROUPS}, now=0)
    return body


def test_ten_thousand_businesses_are_grouped_and_split_as_the_map_zooms(browser, site):
    """With 10,000 saved businesses the map stays quick: zoomed out they are circles with their
    counts (not one solid mass), which split into single pins, in their colours, as the map
    is zoomed in; a pin still opens its details, and businesses at one address open a list."""
    import json
    import time

    body = _many_pins(10_000)
    context = _context(browser, viewport={"width": 1280, "height": 900})
    context.route("**/map-data", lambda route: route.fulfill(status=200, content_type="application/json",
                                                             body=json.dumps(body)))
    tab = context.new_page()
    errors = []
    tab.on("pageerror", lambda exc: errors.append(str(exc)))
    tab.goto(site + "#find")
    started = time.monotonic()
    tab.click("nav a[data-page=map]")
    tab.wait_for_function("M.loaded === true", timeout=20000)
    took = time.monotonic() - started
    assert took < 8, took
    for group in ("yes", "no", "unchecked", "competitor"):
        expect(tab.locator(f"#map-n-{group}")).to_have_text(f"({body['counts'][group]:,})")
    # Zoomed out: a few dozen circles hold every business, and few single pins are drawn.
    counted = tab.evaluate("""() => ({ circles: M.clustered.size, shown: M.shown.size,
        held: [...M.clustered.values()].reduce((n, c) => n + c.members.length, 0) })""")
    assert 5 <= counted["circles"] <= 400 and counted["shown"] < 100, counted
    assert counted["held"] + counted["shown"] == 10_000, counted
    expect(tab.locator("#map .pin-cluster")).to_have_count(counted["circles"])
    # Each circle shows its count; the biggest hold hundreds.
    assert tab.evaluate("Math.max(...[...M.clustered.values()].map((c) => c.members.length))") >= 100
    # Regrouping at a new zoom, or as the map moves, takes a moment, not seconds.
    ms = tab.evaluate("""() => { const t = performance.now();
        M.map.setZoom(M.map.getZoom() + 1, { animate: false }); placePins(); return performance.now() - t; }""")
    assert ms < 1000, ms
    # A circle tapped zooms in to its businesses.
    zoom = tab.evaluate("M.map.getZoom()")
    tab.locator("#map .pin-cluster").first.click()
    tab.wait_for_function(f"M.map.getZoom() > {zoom} && !M.map._animatingZoom")   # the zoom has ended
    # Zoomed into a neighbourhood every business is a pin of its own, in its group's colour,
    # and opens its details.
    target = next(p for p in body["pins"] if p[3] == "yes")
    tab.evaluate("([lat, lon]) => M.map.setView([lat, lon], 16, { animate: false })", target[1:3])
    assert tab.evaluate("M.clustered.size") == 0 and tab.evaluate("M.shown.size") >= 1
    colours = tab.evaluate("""() => [...M.shown].filter((p) => p.group === 'yes')
        .map((p) => p.marker.options.fillColor)""")
    yes = tab.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--pin-yes').trim()")
    assert colours and set(colours) == {yes}
    tab.mouse.click(*_on_screen(tab, target[1], target[2]))
    expect(tab.locator("#map .pin-box")).to_contain_text(target[4])
    # Two businesses at one address stay one circle however far in, and it lists them.
    tab.evaluate("M.map.closePopup()")
    expect(tab.locator("#map .pin-box")).to_have_count(0)            # it fades out
    tab.evaluate("M.map.setView([40.9, -112.15], 18, { animate: false })")
    twin = tab.locator("#map .pin-cluster")
    expect(twin).to_have_count(1)
    expect(twin).to_have_text("2")
    twin.click()
    box = tab.locator("#map .pin-box")
    expect(box).to_contain_text("2 businesses at this spot")
    box.get_by_role("button", name="Twin Suite B").click()
    expect(box).to_contain_text("Has a baler or compactor: Yes")
    box.get_by_role("button", name="Back to the list").click()
    expect(box.get_by_role("button", name="Twin Suite A")).to_be_visible()
    context.close()
    assert not errors


@pytest.mark.parametrize("width", [375, 1280])
def test_the_map_keys_headings_sit_inside_their_light_cards(page, width):
    """The owner (2026-10-08): "Businesses" and "Also on the map" sat on the cards' top edge,
    half outside the light area."""
    page.set_viewport_size({"width": width, "height": 900})
    page.click("nav a[data-page=map]")
    page.wait_for_selector(".map-side legend")
    boxes = page.evaluate("""() => [...document.querySelectorAll('.map-side fieldset.card')].map(f => {
        const card = f.getBoundingClientRect(), head = f.querySelector('legend').getBoundingClientRect();
        const border = parseFloat(getComputedStyle(f).borderTopWidth);
        return [f.querySelector('legend').textContent, card.top + border, head.top, card.left, head.left];
    })""")
    assert len(boxes) == 2
    for name, inside_top, head_top, card_left, head_left in boxes:
        assert head_top >= inside_top + 4, name
        assert head_left > card_left, name


def test_not_called_needs_a_confirm_then_puts_the_business_back_on_leads(page):
    """The owner (2026-10-08): a called business can be put back as never called at any time;
    one click says "Confirm?", a second one (a double click too) does it."""
    from leadgen import calls, marks
    uid = _uid("Smith")
    marks.set_mark(uid, "yes")
    calls.log_call(uid, "Follow Up", "Send a quote.", by="Dana")
    sent = _posts(page, "/withdraw")
    page.reload()
    page.get_by_role("button", name="Has baler or compactor (1)").click()
    row = _row(page, "Smith")
    button = row.get_by_role("button", name="Not called: Smith's Marketplace", exact=True)
    expect(button).to_have_text("Not called")
    # One click only asks; nothing is sent. Escape, a click elsewhere or 5 seconds take it back.
    button.click()
    asking = row.locator("button.not-called.confirm")
    expect(asking).to_have_text("Confirm?")
    assert "danger" in asking.get_attribute("class")
    page.keyboard.press("Escape")
    expect(button).to_have_text("Not called")
    button.click()
    expect(asking).to_have_text("Confirm?")
    page.click("#page-leads h1")
    expect(button).to_have_text("Not called")
    button.click()
    expect(asking).to_have_text("Confirm?")
    expect(button).to_have_text("Not called", timeout=7000)
    page.wait_for_timeout(300)
    assert sent == [] and len(calls.history(uid)) == 1
    # A double click is the click and its confirm.
    button.dblclick()
    expect(row.locator("button.not-called")).to_have_count(0)
    expect(row.locator(".call-last")).to_have_count(0)
    expect(row.locator(".mark button.yes.on")).to_have_count(1)           # its Yes stays
    assert len(sent) == 1 and calls.history(uid) == []
    expect(page.locator("#toast")).to_contain_text("back as not called")
    page.click("nav a[data-page=calls]")
    expect(page.locator("#calls-wrap")).to_contain_text("No calls logged yet.")


def test_not_called_on_the_calls_page_with_the_keyboard(page):
    from leadgen import calls
    smith, costco = _uid("Smith"), _uid("Costco")
    calls.log_call(costco, "Interested", "Wants a baler.", by="Dana")
    calls.log_call(smith, "No Contact", "", by="Dana")
    page.click("nav a[data-page=calls]")
    page.wait_for_selector("table.calls")
    cell = page.locator("#calls-wrap tr", has_text="Smith's Marketplace")
    button = cell.get_by_role("button", name="Not called: Smith's Marketplace", exact=True)
    button.focus()
    page.keyboard.press("Enter")
    expect(cell.locator("button.not-called")).to_have_text("Confirm?")
    page.keyboard.press("Escape")
    expect(button).to_have_text("Not called")
    button.focus()
    page.keyboard.press("Enter")
    expect(cell.locator("button.not-called")).to_have_text("Confirm?")
    page.keyboard.press("Space")
    expect(page.locator("#calls-wrap tr", has_text="Smith's Marketplace")).to_have_count(0)
    expect(page.locator("#calls-wrap tr", has_text="Costco Wholesale")).to_have_count(1)
    expect(page.get_by_role("button", name="All called businesses (1)")).to_be_visible()
    assert calls.history(smith) == [] and len(calls.history(costco)) == 1
    page.click("nav a[data-page=leads]")
    expect(_row(page, "Smith").locator(".call-last")).to_have_count(0)


def test_not_called_and_its_confirm_fit_a_phone_card(browser, site, page):
    from leadgen import calls
    calls.log_call(_uid("Smith"), "Follow Up", "Send a quote.", by="Dana")
    context = _context(browser, viewport={"width": 320, "height": 700}, has_touch=True, is_mobile=True)
    phone = context.new_page()
    phone.goto(site + "#leads")
    phone.wait_for_selector("table.leads")
    button = _row(phone, "Smith").locator("button.not-called")
    sizes = []
    for word in ("Not called", "Confirm?"):
        expect(button).to_have_text(word)
        box = button.bounding_box()
        sizes.append((word, box["width"], box["height"], box["x"] + box["width"]))
        if word == "Not called":
            button.tap()
    wide = phone.evaluate("document.documentElement.scrollWidth > window.innerWidth")
    context.close()
    assert all(w >= 44 and h >= 44 and right <= 320 for _, w, h, right in sizes) and not wide, sizes
