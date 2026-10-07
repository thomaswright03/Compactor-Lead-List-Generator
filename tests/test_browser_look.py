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


def test_the_toast_leaves_the_next_row_free_on_a_laptop(page):
    page.set_viewport_size({"width": 1440, "height": 900})
    names = page.locator("table.leads tbody tr td.c-name strong").all_inner_texts()
    first, second = names[0], names[1]
    _row(page, first).locator(".mark button.yes").click()
    expect_toast = page.locator("#toast")
    expect_toast.wait_for(state="visible")
    assert f"{first}: marked Yes." in expect_toast.inner_text()
    row = _row(page, second)
    controls = row.locator("a, button, summary")
    assert controls.count() >= 5
    # Wherever the window is scrolled to, the toast is never over the list.
    for where in ("end", "center", "start"):
        for i in range(controls.count()):
            control = controls.nth(i)
            if not control.is_visible():
                continue
            control.evaluate(f"(e) => e.scrollIntoView({{ block: '{where}' }})")
            hit = control.evaluate("""(e) => { const r = e.getBoundingClientRect();
                const top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
                return e === top || e.contains(top); }""")
            assert hit, (where, control.inner_text())
            control.click(trial=True, timeout=2000)        # Playwright's own "would the click land" check
    assert expect_toast.is_visible()
    toast = expect_toast.bounding_box()
    main = page.locator("main").bounding_box()
    assert toast["x"] + toast["width"] <= main["x"]        # in the sidebar, beside the list


def test_the_toast_leaves_room_to_scroll_the_last_card_clear_on_a_phone(browser, site, page):
    context = _context(browser, viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True)
    tab = context.new_page()
    tab.goto(site + "#leads")
    tab.wait_for_selector("table.leads")
    tab.evaluate("toast(\"Smith's Marketplace: marked Yes.\", () => {})")
    tab.wait_for_selector("#toast:not([hidden])")
    tab.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
    tab.wait_for_timeout(100)
    top = tab.locator("#toast").bounding_box()["y"]
    last = tab.locator("table.leads tbody tr").last.locator("a, button, summary")
    for i in range(last.count()):
        box = last.nth(i).bounding_box()
        if box:
            assert box["y"] + box["height"] <= top, last.nth(i).inner_text()
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
