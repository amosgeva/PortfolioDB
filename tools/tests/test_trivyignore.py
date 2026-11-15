""".trivyignore entries are dated, and the scans that read it all read it.

Audit C-6: the allowlist's reasons were written down but undated, so nothing
said when one should be looked at again. Each entry now carries `added`,
`reviewed` and `review-by` dates, and tools/check_trivyignore_dates.py fails
every scan that reads the file -- the security-scan.yml jobs and publish.yml's
pre-publish scans -- once a review-by date passes (QA of PR #124, defect 2:
publish used to apply the allowlist without the deadline check).

The real file's date passing is deliberately *not* a failure here: these tests
run on every pull request, and an allowlist falling due must not redden an
unrelated change. The checker is tested with an explicit `--today` instead.
"""

from __future__ import annotations

import io
import re
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
IGNORE = ROOT / ".trivyignore"
WORKFLOWS = ROOT / ".github" / "workflows"
MAX_REVIEW_INTERVAL = timedelta(days=180)

sys.path.insert(0, str(ROOT / "tools"))
import check_trivyignore_dates as checker  # noqa: E402

_ID = re.compile(r"^(CVE-\d{4}-\d+|GHSA-[\w-]+|AVD-[\w-]+|[A-Z]+-\d{4}-\d+)$")


def _entries() -> list[tuple[str, dict[str, date]]]:
    return checker.parse_entries(IGNORE.read_text(encoding="utf-8"))


def _run_checker(monkeypatch, text: str, today: str) -> int:
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))
    return checker.main(["--today", today])


DATED = "# added: 2026-09-09\n# reviewed: 2026-11-15\n# review-by: 2027-02-15\nCVE-2025-47273\n"


@pytest.mark.parametrize("today,expected", [("2027-02-14", 0), ("2027-02-15", 0), ("2027-02-16", 1)])
def test_checker_fails_only_once_the_review_date_has_passed(monkeypatch, today, expected):
    assert _run_checker(monkeypatch, DATED, today) == expected


def test_checker_fails_an_entry_with_no_review_date(monkeypatch, capsys):
    assert _run_checker(monkeypatch, "# added: 2026-09-09\nCVE-2025-47273\n", "2026-11-15") == 1
    assert "no `# review-by:` date" in capsys.readouterr().out


def test_checker_accepts_an_empty_allowlist(monkeypatch):
    assert _run_checker(monkeypatch, "# nothing allowlisted\n", "2030-01-01") == 0


def test_checker_passes_the_real_file_on_the_day_it_was_reviewed(monkeypatch):
    assert _run_checker(monkeypatch, IGNORE.read_text(encoding="utf-8"), "2026-11-15") == 0


def test_every_entry_is_an_advisory_id():
    for ident, _ in _entries():
        assert _ID.match(ident), f".trivyignore line {ident!r} is not an advisory ID"


@pytest.mark.parametrize("ident,dates", _entries(), ids=lambda v: v if isinstance(v, str) else "")
def test_every_entry_is_dated_with_a_bounded_review(ident, dates):
    assert {"added", "reviewed", "review-by"} <= set(dates), (
        f"{ident}: needs `# added:`, `# reviewed:` and `# review-by:` lines directly above it"
    )
    assert dates["added"] <= dates["reviewed"] < dates["review-by"], ident
    assert dates["review-by"] - dates["reviewed"] <= MAX_REVIEW_INTERVAL, (
        f"{ident}: review-by is more than 180 days after the last review"
    )


# The allowlist a scan may read: the checkout's own (security-scan.yml), or in
# publish.yml the copy its policy step extracts from the workflow's commit,
# because a rebuild checks out an older release whose file may be undated.
_ALLOWED_IGNOREFILES = {".trivyignore", "${{ steps.policy.outputs.dir }}/.trivyignore"}


def _jobs_with_trivy():
    for wf in ("security-scan.yml", "publish.yml"):
        doc = yaml.safe_load((WORKFLOWS / wf).read_text(encoding="utf-8"))
        for job_id, job in doc["jobs"].items():
            steps = job.get("steps", [])
            if any("trivy-action" in str(s.get("uses", "")) for s in steps):
                yield f"{wf}:{job_id}", steps


def _trivy_steps():
    for where, steps in _jobs_with_trivy():
        for step in steps:
            if "trivy-action" in str(step.get("uses", "")):
                yield f"{where}:{step.get('name', '')}", step["with"]


def test_every_trivy_scan_applies_the_same_policy():
    """One policy everywhere: a stricter publish than scan (or the reverse) means
    an image that passes one gate and fails the other."""
    steps = list(_trivy_steps())
    assert len(steps) >= 4, "expected the PR scan, the published scans and both pre-publish scans"
    for where, with_ in steps:
        assert with_.get("severity") == "HIGH,CRITICAL", where
        assert with_.get("ignore-unfixed") is True, where
        assert with_.get("trivyignores") in _ALLOWED_IGNOREFILES, where
        assert str(with_.get("exit-code")) == "1", where


def test_every_job_that_scans_checks_the_review_dates_first():
    """Defect 2: an expired exception must stop a publish as it fails a scan."""
    jobs = dict(_jobs_with_trivy())
    assert {"publish.yml:publish", "security-scan.yml:image", "security-scan.yml:published"} <= set(jobs)
    for where, steps in jobs.items():
        first_scan = next(i for i, s in enumerate(steps) if "trivy-action" in str(s.get("uses", "")))
        checks = [i for i, s in enumerate(steps) if "check_trivyignore_dates.py" in str(s.get("run", ""))]
        assert checks, f"{where} applies .trivyignore without the review-date check"
        assert min(checks) < first_scan, f"{where} checks the dates only after scanning"


def test_publish_takes_its_policy_from_the_workflow_commit():
    doc = yaml.safe_load((WORKFLOWS / "publish.yml").read_text(encoding="utf-8"))
    policy = next(s for s in doc["jobs"]["publish"]["steps"] if s.get("id") == "policy")
    run = policy["run"]
    assert 'git show "${GITHUB_SHA}:${f}"' in run
    assert ".trivyignore" in run
    assert "tools/check_trivyignore_dates.py" in run
    assert "tools/provenance_predicate.py" in run
