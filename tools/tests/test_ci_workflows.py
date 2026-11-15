"""Structural guards on the workflows, so the supply-chain fixes stay fixed.

Each assertion names the audit finding it keeps closed. They read the YAML, not
the prose around it: a comment saying "scanned before push" is worth nothing if
the push step moved above the scan. Nothing here can prove a workflow *runs*
(only GitHub can); what it can prove is that the steps, permissions and order
that make the claims true are still in the file.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WF = ROOT / ".github" / "workflows"
DOCKERFILE = ROOT / "app" / "Dockerfile"
COMPOSE = ROOT / "docker-compose.yml"
DEPENDABOT = ROOT / ".github" / "dependabot.yml"


def _load(path: Path) -> dict:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    # PyYAML reads the bare key `on` as boolean True.
    if True in doc:
        doc["on"] = doc.pop(True)
    return doc


def _workflows() -> dict[str, dict]:
    return {p.name: _load(p) for p in sorted(WF.glob("*.yml"))}


def _steps(job: dict) -> list[dict]:
    return job.get("steps", [])


def _run_text(job: dict) -> str:
    return "\n".join(str(s.get("run", "")) for s in _steps(job))


# ── C-6: no job may run unbounded ────────────────────────────────────────────


@pytest.mark.parametrize("name", sorted(p.name for p in WF.glob("*.yml")))
def test_every_job_has_a_timeout(name):
    doc = _workflows()[name]
    for job_id, job in doc["jobs"].items():
        if "uses" in job:          # a reusable-workflow call; its jobs carry their own
            continue
        assert isinstance(job.get("timeout-minutes"), int), f"{name}:{job_id} has no timeout-minutes"


# ── C-3: test jobs run the image's Python on the locked set ──────────────────


def _image_python() -> str:
    m = re.search(r"^FROM python:(\d+\.\d+)-slim@", DOCKERFILE.read_text(encoding="utf-8"), re.MULTILINE)
    assert m, "could not read the image's Python version from app/Dockerfile"
    return m.group(1)


@pytest.mark.parametrize("job_id", ["tests", "database"])
def test_test_jobs_use_the_image_python_and_the_lock(job_id):
    job = _workflows()["ci.yml"]["jobs"][job_id]
    pythons = [s["with"]["python-version"] for s in _steps(job) if "setup-python" in str(s.get("uses", ""))]
    assert pythons == [_image_python()], f"{job_id} runs Python {pythons}, the image runs {_image_python()}"
    installs = [line for line in _run_text(job).splitlines() if "pip install" in line and "requirements.txt" in line]
    assert installs, f"{job_id} installs no requirements"
    for line in installs:
        assert "-c app/constraints.txt" in line, f"{job_id} installs without the lock: {line.strip()}"
        assert "--only-binary :all:" in line, f"{job_id} allows sdists: {line.strip()}"


# ── T-1: every database-backed test runs against a database ──────────────────


def test_database_job_runs_whole_suites_with_the_database_required():
    job = _workflows()["ci.yml"]["jobs"]["database"]
    required = [s for s in _steps(job) if (s.get("env") or {}).get("PORTFOLIODB_TESTS_REQUIRE_DB") == "1"]
    runs = {(s.get("working-directory"), s["run"].split("$COV_ARGS")[0].strip()) for s in required}
    assert ("app", "python -m pytest tests/ -q -rs") in runs, "the whole app suite must run with a database"
    assert (None, 'python -m pytest app/mcp/tests/ -m "not slow" -q -rs') in runs, (
        "the whole MCP fast suite must run with a database"
    )
    assert any("-m slow" in str(s.get("run", "")) for s in _steps(job)), "the reconciliation suite"
    assert "--cov" in str(job.get("env", {}).get("COV_ARGS", "")), "coverage is measured in the database job"


def test_ci_postgres_is_the_image_compose_runs():
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    ci = _workflows()["ci.yml"]["jobs"]["database"]["services"]["postgres"]["image"]
    assert ci == compose["services"]["postgres"]["image"], (
        "ci.yml tests a different Postgres than docker-compose.yml runs"
    )


def test_lint_job_runs_the_in_repo_ruff_config():
    job = _workflows()["ci.yml"]["jobs"]["lint"]
    assert "ruff check --config tools/ruff.toml" in _run_text(job)


# ── C-1 / C-5: scanned before push, signed, attested, rebuilt ────────────────


def _publish_job() -> dict:
    return _workflows()["publish.yml"]["jobs"]["publish"]


def test_publish_scans_both_architectures_before_it_pushes():
    steps = _steps(_publish_job())
    names = [s.get("name", "") for s in steps]
    push_at = next(i for i, s in enumerate(steps) if (s.get("with") or {}).get("push") is True)
    trivy_at = [i for i, s in enumerate(steps) if "trivy-action" in str(s.get("uses", ""))]
    scanned_refs = {steps[i]["with"]["image-ref"] for i in trivy_at}
    assert scanned_refs == {"portfoliodb-prepublish:amd64", "portfoliodb-prepublish:arm64"}, names
    assert all(i < push_at for i in trivy_at), "a Trivy scan runs after the push"
    for i in trivy_at:
        assert str(steps[i]["with"]["exit-code"]) == "1", "a pre-publish scan that cannot fail gates nothing"
    # The scanned builds must be fresh: a cached apt layer would ship old Debian.
    loads = [s for s in steps if (s.get("with") or {}).get("load") is True]
    assert len(loads) == 2
    assert all(s["with"].get("no-cache") is True for s in loads)


def test_publish_attaches_sbom_and_provenance_and_signs():
    steps = _steps(_publish_job())
    push = next(s for s in steps if (s.get("with") or {}).get("push") is True)
    assert push["with"].get("sbom") is True
    assert str(push["with"].get("provenance")).startswith("mode="), "provenance must be on"
    run = _run_text(_publish_job())
    assert "cosign sign --yes --recursive" in run
    assert "cosign verify" in run, "the workflow verifies its own signature the way a user would"
    # The GitHub provenance is a custom SLSA v1 predicate that names the built
    # checkout (QA of PR #124, defect 1), not the stock action, whose predicate
    # only knows the workflow's commit.
    assert not any("attest-build-provenance" in str(s.get("uses", "")) for s in steps)
    attest = next(s for s in steps if str(s.get("uses", "")).startswith("actions/attest@"))
    assert attest["with"]["predicate-type"] == "https://slsa.dev/provenance/v1"
    assert attest["with"]["predicate-path"] == "${{ runner.temp }}/provenance.json"
    assert attest["with"]["subject-digest"] == "${{ steps.push.outputs.digest }}"
    write = next(s for s in steps if s.get("name", "").startswith("Write the provenance predicate"))
    assert "provenance_predicate.py" in write["run"]
    assert write["env"]["BUILT_COMMIT"] == "${{ steps.policy.outputs.built }}"
    names = [s.get("name", "") for s in steps]
    assert names.index(write["name"]) < names.index(attest["name"])


def test_only_the_publish_job_can_mint_an_oidc_token_or_push():
    doc = _workflows()["publish.yml"]
    assert doc["permissions"] == {"contents": "read"}
    for job_id, job in doc["jobs"].items():
        perms = job.get("permissions") or {}
        if job_id == "publish":
            assert perms.get("id-token") == "write"
            assert perms.get("packages") == "write"
        else:
            assert "id-token" not in perms, job_id
            assert "packages" not in perms, job_id


def test_publish_rebuilds_on_a_schedule_and_honours_skip_publish():
    doc = _workflows()["publish.yml"]
    assert doc["on"].get("schedule"), "no scheduled rebuild: :1 and latest would go stale between releases"
    plan = _run_text(doc["jobs"]["plan"])
    # A tag is decided before the skip marker is even looked at.
    tag_branch = plan.index('[[ "$GITHUB_REF" == refs/tags/v* ]]')
    skip_branch = plan.index('[[ "$HEAD_MESSAGE" == *"[skip publish]"* ]]')
    assert tag_branch < skip_branch, "a tag must publish even if its commit says [skip publish]"
    assert "HEAD_MESSAGE" in plan
    assert "${{ github.event.head_commit.message }}" not in plan, (
        "the commit message must reach the script through env, never pasted into it"
    )


def test_published_images_are_scanned_on_a_schedule_by_digest():
    doc = _workflows()["security-scan.yml"]
    job = doc["jobs"]["published"]
    assert set(job["strategy"]["matrix"]["tag"]) == {"latest", "1"}
    assert set(job["strategy"]["matrix"]["platform"]) == {"linux/amd64", "linux/arm64"}
    assert "imagetools inspect" in _run_text(job)
    assert ".digest" in _run_text(job)
    assert doc["on"].get("schedule")


def test_a_failing_scheduled_scan_opens_a_tracking_issue():
    job = _workflows()["security-scan.yml"]["jobs"]["track"]
    assert job["permissions"] == {"issues": "write"}
    assert set(job["needs"]) == {"python-packages", "image", "published"}
    assert "gh issue create" in _run_text(job)
    assert "gh issue close" in _run_text(job)


# ── supply-chain hygiene across every workflow ───────────────────────────────


def test_every_action_is_pinned_to_a_commit():
    """Third-party actions always were; GitHub's own actions/* since 1.8.0 (C-6).

    A tag is mutable, and the publish job can push and sign as this repository.
    Only a local reusable workflow (./...) is exempt: it is this repo's code.
    """
    for name, doc in _workflows().items():
        for job_id, job in doc["jobs"].items():
            for step in _steps(job):
                uses = step.get("uses")
                if not uses or uses.startswith("./"):
                    continue
                assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", uses), (
                    f"{name}:{job_id} uses {uses!r}; pin every action to a full commit SHA"
                )


# ── P-2: Dependabot proposes digest refreshes, not interpreter upgrades ──────


def _dependabot_updates() -> dict[str, dict]:
    doc = yaml.safe_load(DEPENDABOT.read_text(encoding="utf-8"))
    return {u["package-ecosystem"]: u for u in doc["updates"]}


def test_dependabot_never_proposes_a_new_python_minor_or_major():
    docker = _dependabot_updates()["docker"]
    rules = {i["dependency-name"]: set(i["update-types"]) for i in docker.get("ignore", [])}
    assert {"version-update:semver-minor", "version-update:semver-major"} <= rules.get("python", set())


def test_dependabot_keeps_the_compose_images_current_but_never_a_postgres_major():
    compose = _dependabot_updates()["docker-compose"]
    rules = {i["dependency-name"]: set(i["update-types"]) for i in compose.get("ignore", [])}
    assert "version-update:semver-major" in rules.get("postgres", set()), (
        "a Postgres major needs pg_upgrade; Dependabot must not propose it as a bump"
    )
