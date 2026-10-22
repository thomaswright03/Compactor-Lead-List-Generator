"""The page's motion (app.css, Motion; core.js): pages, rows, dialogs, the toast, the sidebar's
gliding pill and the theme's circle run when motion is welcome, and none of it does when the
system asks for reduced motion. Uses test_browser.py's browser, site and sample leads.

Needs Playwright and its Chromium, like test_browser.py (skipped without them).
"""

import pytest

pytest.importorskip("playwright.sync_api")

from playwright.sync_api import expect

import test_browser

browser, site, page = test_browser.browser, test_browser.site, test_browser.page
_context, _row = test_browser._context, test_browser._row

# The animations running on what a selector picks (only those that end: not the spinners).
MOVING = """(sel) => [...document.querySelectorAll(sel)].flatMap((n) => n.getAnimations())
    .filter((a) => a.effect.getComputedTiming().iterations !== Infinity).length"""
# Every entrance on the page has landed (the spinners and glows that loop are left out).
SETTLED = """() => Promise.allSettled(document.getAnimations()
    .filter((a) => a.effect.getComputedTiming().iterations !== Infinity).map((a) => a.finished))"""


def _tab(browser, site, reduced=False):
    context = _context(browser, motion=True, viewport={"width": 1280, "height": 900},
                       reduced_motion="reduce" if reduced else "no-preference")
    tab = context.new_page()
    errors = []
    tab.on("pageerror", lambda exc: errors.append(str(exc)))
    tab.goto(site + "#leads")
    tab.wait_for_selector("table.leads")
    return context, tab, errors


def test_pages_rows_dialogs_and_the_toast_move_into_place(browser, site, page):
    context, tab, errors = _tab(browser, site)
    # A newly opened list: its rows come in one after another.
    rows = tab.locator("table.leads tbody tr")
    assert rows.count() >= 3 and tab.locator("table.leads tbody tr.enter").count() == rows.count()
    tab.evaluate(SETTLED)
    # Rows redrawn after a click keep still; the toast springs up with its countdown line.
    _row(tab, "Costco").locator(".mark button.yes").click()
    expect(tab.locator("#toast")).to_contain_text("marked Yes")
    assert tab.evaluate(MOVING, "#toast") >= 1
    assert tab.evaluate("getComputedStyle(document.getElementById('toast'), '::after').animationName") == "countdown"
    tab.evaluate(SETTLED)
    tab.click("#lead-tabs button:has-text('Has baler')")
    expect(tab.locator("table.leads tbody tr.enter")).to_have_count(1)
    # Another page: its parts wipe up into place.
    tab.click("nav a[data-page=find]")
    assert tab.evaluate(MOVING, "#page-find > *") >= 1
    tab.evaluate(SETTLED)
    # Nothing keeps a transform or clip once it has landed.
    left = tab.evaluate("""() => [...document.querySelectorAll('#page-find > *, #page-find .page-head')]
        .filter((n) => { const s = getComputedStyle(n); return s.transform !== 'none' || s.clipPath !== 'none'; })
        .map((n) => n.id || n.className)""")
    assert left == []
    # A dialog opens like an iris.
    tab.click("#me-change")
    expect(tab.locator("#name-dlg")).to_be_visible()
    assert tab.evaluate(MOVING, "#name-dlg") >= 1
    tab.click("#name-cancel")
    context.close()
    assert not errors


def test_the_sidebar_pill_glides_to_the_page_opened(browser, site, page):
    context, tab, errors = _tab(browser, site)
    glide = tab.locator(".nav-glide")
    expect(glide).to_have_count(1)
    expect(glide).to_have_attribute("aria-hidden", "true")

    def lined_up(name):
        return tab.evaluate("""(name) => {
            const g = document.querySelector('.nav-glide').getBoundingClientRect();
            const a = document.querySelector(`nav a[data-page=${name}]`).getBoundingClientRect();
            return Math.abs(g.top - a.top) < 1 && Math.abs(g.height - a.height) < 1; }""", name)

    tab.evaluate("() => new Promise((r) => setTimeout(r, 600))")
    assert lined_up("leads")
    tab.click("nav a[data-page=stats]")
    tab.evaluate("() => new Promise((r) => setTimeout(r, 600))")    # it springs over in under 0.5 s
    assert lined_up("stats")
    context.close()
    assert not errors


def test_the_theme_spreads_from_the_button_and_lands(browser, site, page):
    context, tab, errors = _tab(browser, site)
    tab.click("[data-theme-pick=dark]")
    tab.wait_for_function("document.documentElement.getAttribute('data-theme') === 'dark'")
    expect(tab.locator("[data-theme-pick=dark]")).to_have_attribute("aria-pressed", "true")
    tab.click("[data-theme-pick=light]")
    tab.wait_for_function("document.documentElement.getAttribute('data-theme') === 'light'")
    context.close()
    assert not errors


def test_reduced_motion_keeps_everything_still(browser, site, page):
    context, tab, errors = _tab(browser, site, reduced=True)
    assert tab.locator("table.leads tbody tr.enter").count() == 0
    tab.click("nav a[data-page=find]")
    assert tab.evaluate(MOVING, "#page-find > *, #page-find .page-head") == 0
    tab.click("#me-change")
    expect(tab.locator("#name-dlg")).to_be_visible()
    assert tab.evaluate(MOVING, "#name-dlg") == 0
    tab.click("#name-cancel")
    # The theme changes at once (no circle).
    tab.click("[data-theme-pick=dark]")
    assert tab.evaluate("document.documentElement.getAttribute('data-theme')") == "dark"
    context.close()
    assert not errors
