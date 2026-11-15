"""A scheduled rebuild of a release from before the runtime stage must still build.

QA of PR #131, F1: 1.8.0 split app/Dockerfile into stages and publish.yml named
`target: runtime` on every build. The weekly rebuild, though, checks out the
*newest release tag* (and the revision `latest` points at), and a Dockerfile
from before 1.8.0 — v1.7.7's, at the time — has one unnamed stage, so BuildKit
refused it ("target stage runtime could not be found") and the rebuild of the
release everyone pulls failed before its scan.

publish.yml now decides the target from the checked-out Dockerfile. These tests
run publish.yml's *own* step scripts — the plan step's release selection and
the target step, extracted from the YAML, not reimplemented — in a synthetic
repository whose newest tag predates the stage, and in one where it does not.
They need bash, git and jq (every CI runner has them) and skip elsewhere.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
PUBLISH = ROOT / ".github" / "workflows" / "publish.yml"

pytestmark = pytest.mark.skipif(
    os.name == "nt" or not all(shutil.which(t) for t in ("bash", "git", "jq")),
    reason="runs publish.yml's bash steps; needs bash, git and jq (CI has them)",
)

# The Dockerfile v1.7.7 shipped had this shape: one stage, no name.
LEGACY_DOCKERFILE = "FROM python:3.14-slim\nRUN echo legacy\nCMD [\"true\"]\n"


def _jobs() -> dict:
    return yaml.safe_load(PUBLISH.read_text(encoding="utf-8"))["jobs"]


def _step(job: str, *, name_prefix: str | None = None, step_id: str | None = None) -> dict:
    return next(
        s for s in _jobs()[job]["steps"]
        if (step_id and s.get("id") == step_id) or (name_prefix and s.get("name", "").startswith(name_prefix))
    )


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _outputs(path: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)


def _repo(tmp_path: Path, *, tag_head: str | None = None) -> tuple[Path, str]:
    """v1.7.7 with the legacy Dockerfile; main's head with today's. Returns (repo, head sha)."""
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    (repo / "app" / "Dockerfile").write_text(LEGACY_DOCKERFILE)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "1.7.7")
    _git(repo, "tag", "v1.7.7")
    shutil.copy(ROOT / "app" / "Dockerfile", repo / "app" / "Dockerfile")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "main head")
    if tag_head:
        _git(repo, "tag", tag_head)
    return repo, _git(repo, "rev-parse", "HEAD")


def _plan_rebuild(repo: Path, tmp_path: Path, head: str) -> list[dict]:
    """Run the plan step as the Sunday schedule does; return its matrix entries."""
    script = (_step("plan", step_id="plan")["run"]
              .replace("${{ github.repository_owner }}", "amosgeva")
              .replace("${{ !startsWith(github.ref, 'refs/tags/v0.') }}", "true"))
    assert "${{" not in script, "the plan step grew an expression this test does not substitute"
    out, summary = tmp_path / "plan-out", tmp_path / "plan-summary"
    env = {**os.environ, "EVENT": "schedule", "REBUILD": "", "HEAD_MESSAGE": "", "GITHUB_SHA": head,
           "GITHUB_REF": "refs/heads/main", "GITHUB_REF_NAME": "main",
           "GITHUB_OUTPUT": str(out), "GITHUB_STEP_SUMMARY": str(summary)}
    # No registry here, and no network: a stub `docker` fails the `latest`
    # lookup, which the step skips with a warning, leaving the release
    # selection — the half F1 broke.
    stub = tmp_path / "stub-bin"
    stub.mkdir(exist_ok=True)
    (stub / "docker").write_text("#!/bin/sh\nexit 1\n")
    (stub / "docker").chmod(0o755)
    env["PATH"] = f"{stub}{os.pathsep}{env['PATH']}"
    subprocess.run(["bash", "-c", script], cwd=repo, env=env, check=True, capture_output=True, text=True)
    return json.loads(_outputs(out)["matrix"])["include"]


def _target(repo: Path, tmp_path: Path, workflow_sha: str) -> tuple[subprocess.CompletedProcess, dict, str]:
    out, summary = tmp_path / "target-out", tmp_path / "target-summary"
    out.write_text("")
    summary.write_text("")
    env = {**os.environ, "GITHUB_SHA": workflow_sha, "GITHUB_OUTPUT": str(out), "GITHUB_STEP_SUMMARY": str(summary)}
    proc = subprocess.run(["bash", "-c", _step("publish", step_id="target")["run"]],
                          cwd=repo, env=env, capture_output=True, text=True)
    return proc, _outputs(out), summary.read_text()


def test_rebuilding_a_release_that_predates_the_runtime_stage_builds_its_default_stage(tmp_path):
    repo, head = _repo(tmp_path)
    entries = _plan_rebuild(repo, tmp_path, head)
    release = next(e for e in entries if e["label"].startswith("rebuild v"))
    assert release["ref"] == "v1.7.7", "the schedule rebuilds the newest release tag"

    _git(repo, "checkout", "-q", release["ref"])        # the publish job's checkout
    proc, out, summary = _target(repo, tmp_path, workflow_sha=head)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert out["target"] == "", "a pre-1.8 Dockerfile has no runtime stage to name"
    assert "::warning::" in proc.stdout
    assert "predates the runtime stage" in summary


def test_rebuilding_a_release_with_the_runtime_stage_builds_runtime(tmp_path):
    repo, head = _repo(tmp_path, tag_head="v1.8.0")
    release = next(e for e in _plan_rebuild(repo, tmp_path, head) if e["label"].startswith("rebuild v"))
    assert release["ref"] == "v1.8.0"
    _git(repo, "checkout", "-q", release["ref"])
    proc, out, _ = _target(repo, tmp_path, workflow_sha=head)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert out["target"] == "runtime", "a release with the stage must publish pip-less"


def test_the_workflow_commit_itself_must_have_the_runtime_stage(tmp_path):
    """A push or tag builds the workflow's own commit; a Dockerfile there without
    the stage would ship pip again, so the step fails instead of falling back."""
    repo, _ = _repo(tmp_path)
    _git(repo, "checkout", "-q", "v1.7.7")
    legacy = _git(repo, "rev-parse", "HEAD")
    proc, out, _ = _target(repo, tmp_path, workflow_sha=legacy)
    assert proc.returncode != 0
    assert "no runtime stage" in proc.stdout
    assert "target" not in out


def test_every_build_uses_the_detected_target_and_detection_comes_first():
    steps = _jobs()["publish"]["steps"]
    ids = [s.get("id") for s in steps]
    builds = [i for i, s in enumerate(steps) if "build-push-action" in str(s.get("uses", ""))]
    assert len(builds) == 3
    assert ids.index("target") < min(builds)
    assert ids.index("policy") < ids.index("target"), "detection reads the checkout the policy step verified"
    for i in builds:
        assert steps[i]["with"]["target"] == "${{ steps.target.outputs.target }}"
