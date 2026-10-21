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
    # Protecting main is the owner's GitHub setting (on since 2026-10-03): the runbook gives
    # the steps with the checks' exact names and says it is on, and the README says the same
    # status, names the checks and links to the steps.
    assert "**Require status checks to pass**" in runbook and "**Include default branch**" in runbook
    assert "**Status: on since 2026-10-03.**" in runbook
    readme = " ".join(_read("README.md").split())
    assert "**`main` is protected (since 2026-10-03).**" in readme and "not set yet" not in readme
    assert "docs/operator-runbook.md#deploying" in readme
    for text in (runbook, readme):
        assert all(f"`{job}`" in text for job in ("lint", "test (sqlite)", "test (postgres)"))
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
    """Protecting main, the login page's contact and the old branches were the owner's to do:
    the runbook lists them in one place, each with exact steps (or its done date) and a way
    to check it."""
    runbook = _read("docs/operator-runbook.md")
    section = runbook.split("## Owner actions still open", 1)[1].split("\n## ", 1)[0]
    text = " ".join(section.split())
    assert "--jq .protected" in text and "`lint`, `test (sqlite)` and `test (postgres)`" in text
    assert "`LEADGEN_SUPPORT_CONTACT`" in text and "(tel|mailto)" in text and "`SUPPORT_CONTACT`" in text
    assert "wip-yelp-cap-and-baler-marks" in text and "git ls-remote --heads origin" in text
    assert "**Delete the two old branches** (GitHub; **Done (2026-10-05)**)" in runbook
    assert "docs/operator-runbook.md#owner-actions-still-open" in _read("README.md")


def test_the_runbooks_branch_check_expects_merged_branches_and_flags_unmerged_work():
    """The runbook names the branches that stay (main and claude-staging) and checks the rest
    by whether they are merged into main, so a merged branch waiting to be deleted (ui-revamp,
    2026-10-06) doesn't make its own check fail; anything unmerged still shows."""
    runbook = _read("docs/operator-runbook.md")
    owner = " ".join(runbook.split("## Owner actions still open", 1)[1].split("\n## ", 1)[0].split())
    branches = " ".join(runbook.split("**Branches** (checked", 1)[1].split("\n## ", 1)[0].split())
    for text in (owner, branches):
        assert "`refs/heads/main` and `refs/heads/claude-staging`" in text
        assert "git fetch --prune origin" in text and "git branch -r --no-merged origin/main" in text
        assert "already merged into `main`" in text and "open pull request" in text
        assert "lists only" not in text and "has only" not in text
        assert "`ui-revamp`" in text and "harmless" in text
    assert "**kept on purpose**" in owner and "**kept on purpose**" in branches
    assert "ask whoever made it before deleting it" in branches


def test_the_example_settings_file_has_every_setting_the_site_and_docs_rely_on():
    """Every setting Render is told about is in .env.example, each under a comment saying what
    it does; the administrator's password says what it unlocks."""
    example = _read(".env.example").splitlines()
    keys = re.findall(r"^\s*- key: (\w+)", _read("render.yaml"), re.M)
    assert "ADMIN_PASSWORD" in keys
    for key in keys:
        if key == "PYTHON_VERSION":
            continue
        assert f"{key}=" in example, key
        i = example.index(f"{key}=")
        above = example[i - 1]
        assert above.startswith("# ") or re.fullmatch(r"\w+=", above), (key, above)
    admin = example.index("ADMIN_PASSWORD=")
    assert re.fullmatch(r'# Unlocks "For the site administrator".*', example[admin - 1])
    assert example[admin].endswith("=") and example[admin] == "ADMIN_PASSWORD="          # no value in the file
    for doc in ("README.md", "docs/operator-runbook.md"):
        assert "ADMIN_PASSWORD" in _read(doc), doc


def test_render_checks_healthz_and_nothing_expects_it_always_to_be_200():
    """/healthz answers 503 ("ok": false) while the database is down (tests/test_health.py):
    Render's health check reads it, the runbook says what ok means, and the keep-awake job
    counts a 503 with "ok": false as the site answering rather than failing on it."""
    assert re.search(r"^\s*healthCheckPath:\s*/healthz\s*$", _read("render.yaml"), re.M)
    runbook = " ".join(_read("docs/operator-runbook.md").split())
    assert "| `ok` | `true` (HTTP 200)" in runbook and "`false` (HTTP 503)" in runbook
    awake = _read(".github/workflows/keep-awake.yml")
    assert "curl -f" not in awake and '"503"' in awake and "ok\": ?false" in awake


def test_the_backups_documented_time_is_the_one_github_keeps():
    """GitHub starts the scheduled backup hours late (8 AM to 12:30 PM Utah time in the first
    week of October 2026): the workflow and the runbook say so instead of promising 3 AM."""
    workflow = " ".join(_read(".github/workflows/backup.yml").split())
    runbook = " ".join(_read("docs/operator-runbook.md").split("## Backups and restoring", 1)[1].split())
    for text in (workflow, runbook):
        assert "once a day" in text.lower() and "usually by late morning Utah time" in text
        assert "nobody is searching" not in text and "runs at about 3 AM" not in text


def test_every_page_sends_the_browser_protections(monkeypatch):
    """Site scanners flag a site without these: no framing, no type guessing, HTTPS only."""
    from leadgen import web
    for on_render in (False, True):
        if on_render:
            monkeypatch.setenv("RENDER", "true")
        client = web.create_app(password="pw").test_client()
        for path in ("/login", "/admin", "/healthz", "/static/favicon.svg", "/no-such-page"):
            headers = client.get(path).headers
            assert headers["X-Content-Type-Options"] == "nosniff"
            assert headers["X-Frame-Options"] == "DENY"
            assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
            assert headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
            assert ("Strict-Transport-Security" in headers) == on_render
