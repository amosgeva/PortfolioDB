"""The signed provenance must name the commit the image was built from.

QA of PR #124, defect 1: the weekly rebuild runs publish.yml from main's head
(commit A) but builds another commit (B: the revision `latest` already points
at, or the newest release tag). The stock attest-build-provenance predicate is
built from OIDC claims, so it recorded A as the only source and never B.

Two layers of test. The unit tests drive tools/provenance_predicate.py with
distinct workflow and build SHAs. The end-to-end test then runs publish.yml's
*own* step scripts -- extracted from the YAML, not reimplemented -- in a
synthetic repository where the workflow commit and the checked-out release
really differ, and asserts on the predicate file those steps hand to
actions/attest, which signs it unchanged.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
PUBLISH = ROOT / ".github" / "workflows" / "publish.yml"

sys.path.insert(0, str(ROOT / "tools"))
import provenance_predicate as pp  # noqa: E402

A = "a" * 40   # the commit the workflow runs from (main's head)
B = "b" * 40   # the commit the image is built from (an older release)

ENV = {
    "GITHUB_SERVER_URL": "https://github.com",
    "GITHUB_REPOSITORY": "amosgeva/PortfolioDB",
    "GITHUB_REPOSITORY_ID": "123",
    "GITHUB_REPOSITORY_OWNER_ID": "456",
    "GITHUB_REF": "refs/heads/main",
    "GITHUB_SHA": A,
    "GITHUB_WORKFLOW_REF": "amosgeva/PortfolioDB/.github/workflows/publish.yml@refs/heads/main",
    "GITHUB_EVENT_NAME": "schedule",
    "GITHUB_RUN_ID": "789",
    "GITHUB_RUN_ATTEMPT": "1",
    "RUNNER_ENVIRONMENT": "github-hosted",
}


def _commits(predicate: dict) -> dict[str, str]:
    return {d["name"]: d["digest"]["gitCommit"] for d in predicate["buildDefinition"]["resolvedDependencies"]}


def test_a_rebuild_names_both_the_workflow_source_and_the_built_source():
    p = pp.build_predicate(ENV, B, "v1.7.7")
    assert _commits(p) == {"workflow": A, "source": B}
    source = next(d for d in p["buildDefinition"]["resolvedDependencies"] if d["name"] == "source")
    assert source["uri"] == "git+https://github.com/amosgeva/PortfolioDB@refs/tags/v1.7.7"


def test_the_builder_identity_stays_the_workflow_that_ran():
    """Adding the built source must not make the workflow claim to be anything else."""
    p = pp.build_predicate(ENV, B, B)
    assert p["runDetails"]["builder"]["id"] == (
        "https://github.com/amosgeva/PortfolioDB/.github/workflows/publish.yml@refs/heads/main"
    )
    wf = p["buildDefinition"]["externalParameters"]["workflow"]
    assert wf == {"ref": "refs/heads/main", "repository": "https://github.com/amosgeva/PortfolioDB",
                  "path": ".github/workflows/publish.yml"}
    assert p["buildDefinition"]["buildType"] == "https://actions.github.io/buildtypes/workflow/v1"
    assert p["runDetails"]["metadata"]["invocationId"].endswith("/actions/runs/789/attempts/1")
    workflow_dep = p["buildDefinition"]["resolvedDependencies"][0]
    assert workflow_dep["uri"] == "git+https://github.com/amosgeva/PortfolioDB@refs/heads/main"


def test_a_push_names_the_same_commit_twice_and_says_which_role_is_which():
    p = pp.build_predicate(ENV, A, A)
    assert _commits(p) == {"workflow": A, "source": A}


@pytest.mark.parametrize("bad", ["", "abc1234", "B" * 40, A + "0"])
def test_the_built_commit_must_be_a_full_sha(bad):
    with pytest.raises(ValueError, match="built-commit"):
        pp.build_predicate(ENV, bad, "main")


def test_missing_actions_environment_is_an_error_not_an_empty_field():
    env = dict(ENV)
    del env["GITHUB_WORKFLOW_REF"]
    with pytest.raises(ValueError, match="GITHUB_WORKFLOW_REF"):
        pp.build_predicate(env, B, "v1.7.7")


# ── publish.yml's own steps, against a repo where A and B really differ ──────


def _publish_step(name_prefix: str) -> dict:
    job = yaml.safe_load(PUBLISH.read_text(encoding="utf-8"))["jobs"]["publish"]
    return next(s for s in job["steps"] if s.get("name", "").startswith(name_prefix))


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.mark.skipif(os.name == "nt" or not shutil.which("bash") or not shutil.which("git"),
                    reason="runs publish.yml's bash steps; needs bash and git (CI has both)")
def test_publish_steps_attest_the_checked_out_release_not_the_workflow_commit(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    # B: an old release, from before the policy files existed.
    (repo / "README").write_text("old release\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "release")
    _git(repo, "tag", "v1.7.7")
    built = _git(repo, "rev-parse", "HEAD")
    # A: main's head, carrying today's policy.
    (repo / "tools").mkdir()
    for f in ("check_trivyignore_dates.py", "provenance_predicate.py"):
        shutil.copy(ROOT / "tools" / f, repo / "tools" / f)
    # A synthetic allowlist that is in date whenever this runs: the real
    # file's deadline passing must not redden every pull request.
    today = date.today()
    (repo / ".trivyignore").write_text(
        f"# added: {today}\n# reviewed: {today}\n# review-by: {today + timedelta(days=30)}\nCVE-2025-47273\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "main head")
    workflow_sha = _git(repo, "rev-parse", "HEAD")
    assert workflow_sha != built
    # The publish job's checkout: the release, detached.
    _git(repo, "checkout", "-q", "v1.7.7")

    runner_temp = tmp_path / "runner"
    runner_temp.mkdir()
    outputs = tmp_path / "out.txt"
    env = {**os.environ, **ENV, "GITHUB_SHA": workflow_sha, "RUNNER_TEMP": str(runner_temp),
           "GITHUB_OUTPUT": str(outputs)}

    policy = _publish_step("Publishing policy")
    subprocess.run(["bash", "-c", policy["run"]], cwd=repo, env=env, check=True, capture_output=True, text=True)
    out = dict(line.split("=", 1) for line in outputs.read_text().splitlines())
    assert out["built"] == built

    write = _publish_step("Write the provenance predicate")
    step_env = {k: v.replace("${{ matrix.ref }}", "v1.7.7")
                    .replace("${{ steps.policy.outputs.built }}", out["built"])
                    .replace("${{ steps.policy.outputs.dir }}", out["dir"])
                for k, v in write["env"].items()}
    subprocess.run(["bash", "-c", write["run"]], cwd=repo, env={**env, **step_env},
                   check=True, capture_output=True, text=True)

    attest = _publish_step("Attest build provenance")
    assert attest["with"]["predicate-path"] == "${{ runner.temp }}/provenance.json"
    predicate = json.loads((runner_temp / "provenance.json").read_text())
    assert _commits(predicate) == {"workflow": workflow_sha, "source": built}, (
        "the signed predicate must carry the commit the image was built from"
    )
