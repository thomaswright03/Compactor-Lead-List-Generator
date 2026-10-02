"""The deploy settings and the documents that describe them agree: Render deploys a
push to main only once the three CI jobs are green."""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _read(path):
    return (ROOT / path).read_text(encoding="utf-8")


def test_render_waits_for_the_checks_and_the_docs_say_so():
    assert re.search(r"^\s*autoDeployTrigger:\s*checksPass\s*$", _read("render.yaml"), re.M)
    ci = _read(".github/workflows/ci.yml")
    # The three jobs branch protection and Render wait for.
    assert re.search(r"^  lint:", ci, re.M) and re.search(r"^  test:", ci, re.M)
    assert "database: [sqlite, postgres]" in ci
    for doc in ("README.md", "docs/operator-runbook.md", "docs/developer-overview.md"):
        text = " ".join(_read(doc).split())
        assert "checksPass" in text, doc
        assert "every push straight away" not in text or "switch it back" in text, doc
    runbook = " ".join(_read("docs/operator-runbook.md").split())
    assert "Require status checks to pass before merging" in runbook
    # Protecting main is the owner's GitHub setting: the README and the runbook both give
    # the steps with the checks' exact names, and the runbook says whether it is on yet.
    for doc in ("README.md", "docs/operator-runbook.md"):
        text = " ".join(_read(doc).split())
        assert "**Require status checks to pass**" in text and "**Include default branch**" in text, doc
        assert all(f"`{job}`" in text for job in ("lint", "test (sqlite)", "test (postgres)")), doc
    assert re.search(r"\*\*Status \(\d{4}-\d\d-\d\d\): [^*]+\*\*", runbook)


def test_ci_lints_and_type_checks_every_page_script():
    """Each script of the page is linted and type-checked in CI, in the order the page loads them."""
    ci = _read(".github/workflows/ci.yml")
    lint = ci.split("\n  test:")[0]
    assert "npm ci" in lint and "npm run lint:js" in lint and "npm run types:js" in lint
    scripts = sorted(p.name for p in (ROOT / "leadgen" / "static").glob("*.js"))
    loaded = re.findall(r'src="/static/(\w+\.js)', _read("leadgen/templates/index.html"))
    assert sorted(loaded) == scripts
    eslint = re.search(r"const FILES = \[([^\]]+)\]", _read("eslint.config.mjs")).group(1)
    assert re.findall(r'"(\w+\.js)"', eslint) == loaded
    assert re.findall(r'"leadgen/static/(\w+\.js)"', _read("tsconfig.json")) == loaded
    assert '"lint:js": "eslint leadgen/static"' in _read("package.json")
    # The type check is strict (no untyped parameter, no unchecked null), like mypy's.
    assert re.search(r'^\s*"strict": true,', _read("tsconfig.json"), re.M)
    assert not re.search(r'"(noImplicitAny|strictNullChecks)": false', _read("tsconfig.json"))


def test_the_runbook_lists_the_owners_open_steps_with_a_check_for_each():
    """Protecting main, the login page's contact and the old branch are the owner's to do:
    the runbook lists them in one place, each with exact steps and a way to check it."""
    runbook = _read("docs/operator-runbook.md")
    section = runbook.split("## Owner actions still open", 1)[1].split("\n## ", 1)[0]
    text = " ".join(section.split())
    assert "--jq .protected" in text and "`lint`, `test (sqlite)` and `test (postgres)`" in text
    assert "`LEADGEN_SUPPORT_CONTACT`" in text and "(tel|mailto)" in text and "never in this repository" in text
    assert "wip-yelp-cap-and-baler-marks" in text and "git ls-remote --heads origin" in text
    assert "git push origin --delete wip-yelp-cap-and-baler-marks" in runbook
    assert "docs/operator-runbook.md#owner-actions-still-open" in _read("README.md")
