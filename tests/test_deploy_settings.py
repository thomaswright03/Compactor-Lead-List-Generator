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
    assert all(f"`{job}`" in runbook for job in ("lint", "test (sqlite)", "test (postgres)"))
