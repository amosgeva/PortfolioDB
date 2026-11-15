""".trivyignore entries are dated, and the scans that read it all read it.

Audit C-6: the allowlist's reasons were written down but undated, so nothing
said when one should be looked at again. Each entry now carries `added`,
`reviewed` and `review-by` dates; security-scan.yml fails once a review-by date
passes. This keeps the format honest, so that date check has something to read.

The date passing is deliberately *not* a failure here: these tests run on every
pull request, and an allowlist falling due must not redden an unrelated change.
The scheduled scan is where that shows up.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
IGNORE = ROOT / ".trivyignore"
WORKFLOWS = ROOT / ".github" / "workflows"
MAX_REVIEW_INTERVAL = timedelta(days=180)

_ID = re.compile(r"^(CVE-\d{4}-\d+|GHSA-[\w-]+|AVD-[\w-]+|[A-Z]+-\d{4}-\d+)$")
_DATE = re.compile(r"^# (added|reviewed|review-by): (\d{4}-\d{2}-\d{2})\s*$")


def _entries() -> list[tuple[str, dict[str, date]]]:
    out = []
    pending: dict[str, date] = {}
    for line in IGNORE.read_text(encoding="utf-8").splitlines():
        m = _DATE.match(line)
        if m:
            pending[m.group(1)] = date.fromisoformat(m.group(2))
        elif line.strip() and not line.startswith("#"):
            out.append((line.strip(), pending))
            pending = {}
        elif not line.strip():
            pending = {}
    return out


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


def _trivy_steps():
    for wf in ("security-scan.yml", "publish.yml"):
        doc = yaml.safe_load((WORKFLOWS / wf).read_text(encoding="utf-8"))
        for job_id, job in doc["jobs"].items():
            for step in job.get("steps", []):
                if "trivy-action" in str(step.get("uses", "")):
                    yield f"{wf}:{job_id}:{step.get('name', '')}", step["with"]


def test_every_trivy_scan_applies_the_same_policy():
    """One policy everywhere: a stricter publish than scan (or the reverse) means
    an image that passes one gate and fails the other."""
    steps = list(_trivy_steps())
    assert len(steps) >= 4, "expected the PR scan, the published scans and both pre-publish scans"
    for where, with_ in steps:
        assert with_.get("severity") == "HIGH,CRITICAL", where
        assert with_.get("ignore-unfixed") is True, where
        assert with_.get("trivyignores") == ".trivyignore", where
        assert str(with_.get("exit-code")) == "1", where
