"""Every page the site renders is well-formed HTML: no attribute glued to the one before it
(`id="a"aria-label=...`, a parse error browsers only guess their way around), no attribute
given twice, no id used twice, and every element closed, in order. Checked on the rendered
pages (the templates with their data), with Python's own html.parser, so CI catches it
without a validator to install."""

import re
from html.parser import HTMLParser

import pytest

from leadgen import checks, web

# Elements that never have an end tag.
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
# A quoted attribute value must be followed by a space, "/" or the tag's end.
GLUED = re.compile(r"""=\s*("[^"]*"|'[^']*')(?=[^\s/>])""")


class _Checker(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.problems: list[str] = []
        self.open: list[tuple[str, int]] = []
        self.ids: dict[str, int] = {}

    def _where(self) -> int:
        return self.getpos()[0]

    def _start(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        line, raw = self._where(), self.get_starttag_text() or ""
        glued = GLUED.search(raw)
        if glued:
            self.problems.append(f"line {line}: no space after {glued.group(0)!r} in {raw[:80]!r}")
        names = [name for name, _ in attrs]
        for name in sorted({n for n in names if names.count(n) > 1}):
            self.problems.append(f"line {line}: <{tag}> has {name} twice")
        for name, value in attrs:
            if name == "id" and value:
                if value in self.ids:
                    self.problems.append(f"line {line}: id {value!r} used again (first on line {self.ids[value]})")
                self.ids.setdefault(value, line)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag, attrs)
        if tag not in VOID:
            self.open.append((tag, self._where()))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag, attrs)       # <br/>, <path ... /> in an inline svg

    def handle_endtag(self, tag: str) -> None:
        if tag in VOID:
            self.problems.append(f"line {self._where()}: </{tag}> closes an element that has no end tag")
            return
        if not self.open or self.open[-1][0] != tag:
            still = self.open[-1] if self.open else None
            opened = f"<{still[0]}> from line {still[1]} is still open" if still else "nothing is open"
            self.problems.append(f"line {self._where()}: </{tag}> but {opened}")
            if any(t == tag for t, _ in self.open):    # recover: close up to the matching tag
                while self.open and self.open.pop()[0] != tag:
                    pass
            return
        self.open.pop()


def html_problems(page: str) -> list[str]:
    """The page's problems, each with its line; [] for a well-formed page."""
    checker = _Checker()
    checker.feed(page)
    checker.close()
    return checker.problems + [f"<{tag}> from line {line} is never closed" for tag, line in checker.open]


def test_the_checker_catches_what_it_is_for():
    assert html_problems('<!doctype html><html><body><dialog id="d" aria-label="x"></dialog></body></html>') == []
    assert html_problems("<p>a<br><img src=x alt=''><svg><path d='M0'/></svg></p>") == []
    glued = html_problems('<dialog id="switch-dlg"aria-labelledby="switch-title"></dialog>')
    assert len(glued) == 1 and "no space after" in glued[0]
    assert "has class twice" in html_problems('<div class="a" class="b"></div>')[0]
    assert "used again" in html_problems('<i id="x"></i><b id="x"></b>')[0]
    assert html_problems("<div><span></div>") == ["line 1: </div> but <span> from line 1 is still open"]
    assert html_problems("<main><div></main>")[0].endswith("<div> from line 1 is still open")
    assert html_problems("<main><section>") == ["<main> from line 1 is never closed",
                                                "<section> from line 1 is never closed"]
    assert "</br>" in html_problems("<p>a</br></p>")[0]


def _logged_in(admin: str = "open-sesame"):
    client = web.create_app(password="s3cret", username="Matt", admin_password=admin).test_client()
    client.post("/login", data={"username": "Matt", "password": "s3cret"})
    return client


PAGE = {"Accept": "text/html"}


def _pages(monkeypatch):
    """(name, the rendered page) for every template, in the states that change its markup."""
    monkeypatch.setattr(checks, "run_all", lambda recorded=False: [
        checks.Check("yelp", True, True, 0.4, "Answered"),
        checks.Check("google", False, None, None, "Not set up"),
        checks.Check("osm", True, False, 2.1, "Timed out")])
    monkeypatch.delenv("LEADGEN_ALERT_WEBHOOK", raising=False)
    local = web.create_app(password="", admin_password="").test_client()     # no login: the section is open
    yield "index (local, administrator's section open)", local.get("/", headers=PAGE)
    yield "error page (404)", local.get("/no-such-page", headers=PAGE)
    yield "source check", local.post("/admin/check-sources")
    yield "test alert (no webhook)", local.post("/admin/test-alert")
    site = web.create_app(password="s3cret", username="Matt", admin_password="open-sesame").test_client()
    yield "login", site.get("/login", headers=PAGE)
    yield "login (wrong password)", site.post("/login", data={"username": "Matt", "password": "nope"})
    yield "index (logged in, section locked)", _logged_in().get("/", headers=PAGE)
    yield "index (no administrator password)", _logged_in(admin="").get("/", headers=PAGE)
    monkeypatch.setenv("YELP_API_KEY", "test-yelp-key")
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "test-google-key")
    monkeypatch.setenv("LEADGEN_SEARCH_PAUSED", "1")
    yield "index (keys set, searching paused)", _logged_in().get("/", headers=PAGE)


def test_every_rendered_page_is_well_formed_html(monkeypatch):
    seen = 0
    for name, res in _pages(monkeypatch):
        page = res.get_data(as_text=True)
        assert page.lstrip().lower().startswith("<!doctype html>"), (name, res.status_code)
        assert html_problems(page) == [], name
        seen += 1
    assert seen == 9


@pytest.mark.parametrize("attr", ["aria-labelledby", "aria-describedby"])
def test_the_pause_dialog_keeps_its_accessible_name(attr):
    page = web.create_app(password="", admin_password="").test_client().get("/", headers=PAGE).get_data(as_text=True)
    found: dict[str, str | None] = {}

    class _Dialog(HTMLParser):
        def handle_starttag(self, tag, attrs):
            if dict(attrs).get("id") == "switch-dlg":
                found.update(attrs)

    _Dialog().feed(page)
    assert found[attr] == {"aria-labelledby": "switch-title", "aria-describedby": "switch-why"}[attr]
