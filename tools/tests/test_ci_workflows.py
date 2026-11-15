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
    assert set(job["needs"]) == {"python-packages", "image", "published", "llm-models"}
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


def test_dependabot_proposes_weekly_frontend_toolchain_updates_grouped():
    """Section 12: the npm toolchain that builds, lints and tests the bundle had
    no version-update coverage. Minor and patch bumps come grouped; a major
    comes on its own, where config formats and rule semantics change."""
    npm = _dependabot_updates().get("npm")
    assert npm, "Dependabot does not watch package.json / package-lock.json"
    assert npm["directory"] == "/"
    assert (ROOT / "package.json").exists()
    assert (ROOT / "package-lock.json").exists()
    assert npm["schedule"]["interval"] == "weekly"
    groups = list((npm.get("groups") or {}).values())
    assert any(set(g.get("update-types", [])) == {"minor", "patch"} and g.get("patterns") == ["*"] for g in groups), (
        "minor and patch front-end bumps should arrive as one grouped pull request"
    )


# ── NEW-2: the front-end gate is required on PRs and gates publication ───────

# Required on main as a status check: renaming the job silently leaves every PR
# waiting for a check that never reports.
FRONTEND_CHECK = "Build check, lint, tests"
FRONTEND_WORKFLOW = "./.github/workflows/frontend.yml"
CI_WORKFLOW = "./.github/workflows/ci.yml"


def test_frontend_check_keeps_its_required_name_on_pull_requests():
    doc = _workflows()["frontend.yml"]
    assert "pull_request" in doc["on"], "the front-end check must report on every pull request"
    names = [job.get("name") for job in doc["jobs"].values()]
    assert names == [FRONTEND_CHECK], f"frontend.yml jobs are named {names}; branch protection requires {FRONTEND_CHECK!r}"


def test_frontend_workflow_is_reusable_and_read_only():
    doc = _workflows()["frontend.yml"]
    assert "workflow_call" in doc["on"], "publish.yml cannot call the front-end gate"
    assert doc["permissions"] == {"contents": "read"}
    for job_id, job in doc["jobs"].items():
        assert not job.get("permissions"), f"frontend.yml:{job_id} widens the token"
        assert not job.get("continue-on-error"), f"frontend.yml:{job_id} cannot fail"
    run = _run_text(next(iter(doc["jobs"].values())))
    for step in ("npm ci", "npm run build:check", "npm run lint", "npm test"):
        assert step in run, f"the front-end gate no longer runs `{step}`"


@pytest.mark.parametrize("name", ["ci.yml", "frontend.yml"])
def test_called_workflows_key_concurrency_on_the_calling_workflow(name):
    """A called workflow shares github.ref with its caller. Keyed on the ref
    alone, publish's call and the workflow's own push run on main would share a
    group, and cancel-in-progress would cancel one of them."""
    group = _workflows()[name]["concurrency"]["group"]
    assert "${{ github.workflow }}" in group, group
    assert "${{ github.ref }}" in group, group


def _needs(job: dict) -> set[str]:
    needs = job.get("needs") or []
    return {needs} if isinstance(needs, str) else set(needs)


def _ancestors(jobs: dict, job_id: str) -> set[str]:
    seen: set[str] = set()
    todo = list(_needs(jobs[job_id]))
    while todo:
        j = todo.pop()
        if j not in seen:
            seen.add(j)
            todo.extend(_needs(jobs[j]))
    return seen


def _publish_gate_problems(doc: dict) -> list[str]:
    """Why a publish workflow could build or push without both gates green."""
    jobs = doc["jobs"]
    gates = {
        uses: [j for j, job in jobs.items() if job.get("uses") == uses]
        for uses in (CI_WORKFLOW, FRONTEND_WORKFLOW)
    }
    problems = [f"no job calls {uses}" for uses, ids in gates.items() if not ids]
    builders = [
        j for j, job in jobs.items()
        if any(re.search(r"docker/(build-push|login)-action", str(s.get("uses", ""))) for s in _steps(job))
    ]
    if not builders:
        problems.append("no job builds the image")
    for j in builders:
        before = _ancestors(jobs, j)
        for uses, ids in gates.items():
            if ids and not before & set(ids):
                problems.append(f"{j} can build/push without {uses}")
        if re.search(r"always\(\)|failure\(\)|!\s*cancelled\(\)", str(jobs[j].get("if", ""))):
            problems.append(f"{j} runs even when a gate failed")
    for ids in gates.values():
        for g in ids:
            if jobs[g].get("continue-on-error"):
                problems.append(f"gate {g} cannot fail")
    return problems


def test_publish_builds_nothing_until_ci_and_the_frontend_gate_pass():
    """Audit NEW-2: publish.yml waited on ci.yml alone, so a red front end
    (an unescaped innerHTML, a bundle that no longer matches its sources) could
    still be published."""
    assert _publish_gate_problems(_workflows()["publish.yml"]) == []


def test_the_publish_gate_check_catches_a_missing_frontend_dependency():
    """Negative control: the check above must notice the pre-1.8 DAG."""
    doc = _workflows()["publish.yml"]
    regressed = {**doc, "jobs": {**doc["jobs"], "publish": {**doc["jobs"]["publish"], "needs": ["plan", "test"]}}}
    assert any("without ./.github/workflows/frontend.yml" in p for p in _publish_gate_problems(regressed))
    dropped = {**doc, "jobs": {k: v for k, v in doc["jobs"].items() if v.get("uses") != FRONTEND_WORKFLOW}}
    dropped["jobs"]["publish"] = {**dropped["jobs"]["publish"], "needs": ["plan", "test"]}
    assert "no job calls ./.github/workflows/frontend.yml" in _publish_gate_problems(dropped)
    forced = {**doc, "jobs": {**doc["jobs"], "publish": {**doc["jobs"]["publish"], "if": "always()"}}}
    assert any("even when a gate failed" in p for p in _publish_gate_problems(forced))


def test_the_frontend_gate_runs_whenever_publish_would():
    jobs = _workflows()["publish.yml"]["jobs"]
    gate = next(job for job in jobs.values() if job.get("uses") == FRONTEND_WORKFLOW)
    assert gate["if"] == jobs["publish"]["if"], "a skipped gate is a passed gate to `needs`"
    assert not gate.get("permissions"), "the gate inherits the read-only token"


# ── O-2: what ships has no package installer ─────────────────────────────────


def _stages() -> list[str]:
    return re.findall(r"^FROM\s+\S+\s+AS\s+(\w+)\s*$", DOCKERFILE.read_text(encoding="utf-8"), re.MULTILINE)


def test_the_runtime_stage_is_last_and_removes_pip():
    """The last stage is the default target, so a bare `docker build` (the
    security scan's) gets the runtime too."""
    stages = _stages()
    assert stages[-1] == "runtime", stages
    assert "dev" in stages, stages
    text = DOCKERFILE.read_text(encoding="utf-8")
    runtime = text[text.index("AS runtime"):]
    assert "pip uninstall --yes pip" in runtime
    assert "ensurepip/_bundled" in runtime, "ensurepip's bundled wheel would put pip straight back"


def test_publish_builds_and_scans_the_runtime_target():
    steps = _steps(_publish_job())
    builds = [s for s in steps if "build-push-action" in str(s.get("uses", ""))]
    assert len(builds) == 3
    # The stage is detected from the checked-out Dockerfile (QA of #131, F1:
    # a rebuilt pre-1.8 release has no runtime stage); one value for all three
    # builds, so the scanned and pushed images are the same stage.
    assert all(s["with"].get("target") == "${{ steps.target.outputs.target }}" for s in builds)
    detect = next(s for s in steps if s.get("id") == "target")
    assert 'echo "target=runtime"' in detect["run"]
    assert '"$GITHUB_SHA"' in detect["run"], "the workflow's own commit must not fall back to a pip stage"


def test_ci_checks_the_lock_on_dev_and_tests_inside_runtime():
    job = _workflows()["ci.yml"]["jobs"]["image"]
    run = _run_text(job)
    assert "--target runtime" in run
    assert "--target dev" in run
    assert "portfoliodb-ci-dev:${{ github.sha }} pip freeze --all" in run, "the lock is compared on the dev target"
    assert 'python -c "import pip"' in run, "CI must prove the runtime image has no pip"
    tests = next(s for s in _steps(job) if s.get("name", "").startswith("Test suites inside the runtime image"))
    assert "portfoliodb-ci:${{ github.sha }}" in tests["run"]
    assert "$HARDENED" in tests["run"]
    assert "pip install --user" not in tests["run"].split("portfoliodb-ci:${{ github.sha }}")[-1], (
        "the runtime image has no pip; pytest comes from the dev target, mounted read-only"
    )


def test_ci_starts_both_schedulers_and_tests_both_crontabs():
    run = _run_text(_workflows()["ci.yml"]["jobs"]["image"])
    assert "/app/docker/crontab-llm" in run
    assert "probe ci-scheduler-llm" in run
    assert "supercronic -test" in run


DOCKERIGNORE = ROOT / ".dockerignore"


def test_the_image_carries_the_built_front_end_not_its_sources():
    """streamlit_app.py and modern2_native.py read only the committed outputs."""
    lines = {l.strip() for l in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()}
    for excluded in ("node_modules/", "app/dashboard/static/src/", "app/dashboard/static/tests/",
                     "app/dashboard/static/tools/", "app/dashboard/static/build.mjs"):
        assert excluded in lines, f".dockerignore no longer excludes {excluded}"
    served = ROOT / "app" / "dashboard" / "static"
    for needed in ("app.js", "app.css", "shell.html", "skeleton.html", "tokens.css", "fonts"):
        assert (served / needed).exists()
        assert not any(l.rstrip("/") in (f"app/dashboard/static/{needed}", "app/dashboard/static") for l in lines), (
            f".dockerignore drops {needed}, which the dashboard serves"
        )
