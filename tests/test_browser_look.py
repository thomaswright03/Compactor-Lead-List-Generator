"""What the user sees, measured in a real browser: a field with a wrong value, the toast after a
click, and the keyboard's focus ring. Uses test_browser.py's browser, site and sample leads.

Needs Playwright and its Chromium, like test_browser.py (skipped without them).
"""

import pytest

pytest.importorskip("playwright.sync_api")

import test_browser

# test_browser.py's fixtures (a Chromium, the site, and a Leads page with four sample leads) and helpers.
browser, site, page = test_browser.browser, test_browser.site, test_browser.page
_context, _row = test_browser._context, test_browser._row

# The page's error colour, as the browser computes it for a field (the theme's --bad).
BAD = "(() => { const p = document.createElement('i'); p.style.color = 'var(--bad)'; document.body.append(p);"\
      " const c = getComputedStyle(p).color; p.remove(); return c; })()"


def _look(tab, selector):
    return tab.evaluate("""(sel) => { const cs = getComputedStyle(document.querySelector(sel));
        return { border: [cs.borderTopColor, cs.borderRightColor, cs.borderBottomColor, cs.borderLeftColor],
                 shadow: cs.boxShadow, caps: cs.fontVariantCaps, size: cs.fontSize, spacing: cs.letterSpacing,
                 transform: cs.textTransform }; }""", selector)


def _marked_wrong(tab, selector):
    """The field's four edges are the error colour, and it has a soft ring in that colour (once the
    edge's short colour change has run)."""
    bad = tab.evaluate(BAD)
    tab.wait_for_function("([sel, bad]) => getComputedStyle(document.querySelector(sel)).borderTopColor === bad",
                          arg=[selector, bad], timeout=3000)
    look = _look(tab, selector)
    assert look["border"] == [bad] * 4, (selector, look, bad)
    assert "4px" in look["shadow"] and "inset" not in look["shadow"], look
    return bad


def _theme(tab, theme):
    tab.click(f".theme button[data-theme-pick={theme}]")
    tab.wait_for_function(f"document.documentElement.getAttribute('data-theme') === '{theme}'")


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_a_wrong_value_marks_its_field_red_and_says_why_in_plain_text(page, theme):
    _theme(page, theme)
    page.click("nav a[data-page=find]")
    page.wait_for_selector("text=Today's is available")
    typed = "Asdfqwerzxcv" * 6                              # one word over 60 characters
    page.fill("input[name=keywords]", typed)
    page.click("#go")
    error = page.locator("#err-keywords")
    field = "input[name=keywords]"
    assert page.evaluate("document.activeElement.name") == "keywords"
    _marked_wrong(page, field)                              # focused ...
    page.evaluate("document.activeElement.blur()")
    _marked_wrong(page, field)                              # ... and not
    # The message under the field: normal case at reading size, what was typed as typed.
    look = _look(page, "#err-keywords")
    body = page.evaluate("getComputedStyle(document.body).fontSize")
    assert look["caps"] == "normal" and look["transform"] == "none" and look["spacing"] == "normal", look
    assert look["size"] == body
    assert f"“{typed[:20]}…”" in error.inner_text()
    # The other fields keep their normal edge.
    other = _look(page, "input[name=location]")["border"]
    assert other != [page.evaluate(BAD)] * 4


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_an_empty_name_marks_the_name_box_red(browser, site, page, theme):
    context = _context(browser, name=None, viewport={"width": 1280, "height": 900})
    tab = context.new_page()
    tab.goto(site + "#leads")
    tab.wait_for_selector("table.leads")
    _theme(tab, theme)
    _row(tab, "Costco").locator(".mark button.yes").click()
    tab.wait_for_selector("#name-dlg[open]")
    tab.fill("#name-input", "")
    tab.click("#name-save")
    tab.wait_for_selector("#name-error:not([hidden])")
    assert tab.evaluate("document.activeElement.id") == "name-input"
    _marked_wrong(tab, "#name-input")
    tab.evaluate("document.activeElement.blur()")
    _marked_wrong(tab, "#name-input")
    context.close()


def test_explain_gets_the_same_focus_ring_as_every_control(page):
    summary = page.locator("table.leads tbody tr details.explain summary").first
    page.keyboard.press("Shift")                           # the keyboard is in use
    summary.focus()
    ring = summary.evaluate("(e) => { const cs = getComputedStyle(e); return [cs.outlineStyle, cs.outlineWidth]; }")
    button = page.locator("table.leads tbody tr .mark button.yes").first
    button.focus()
    assert ring == ["solid", "3px"]
    assert ring == button.evaluate("(e) => { const cs = getComputedStyle(e); "
                                   "return [cs.outlineStyle, cs.outlineWidth]; }")

