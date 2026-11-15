"""The Makefile's recipes, run for real through make and sh, with Docker stubbed.

`COMPOSE ?= docker compose` is overridable, so each test points it at a small
Python stub that records the argv it was given (and plays Postgres where a
recipe reads from it). What is asserted is the argv the real recipe hands to
compose after the real shell parsed it -- the level at which audit E-7 broke:
`make ask ARGS="what's my concentration risk?"`, the help text's own example,
was a shell syntax error.

Needs GNU make and a POSIX shell, so it skips on Windows; every CI runner has
both. No Docker, no network, no Postgres.
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(
    os.name == "nt" or shutil.which("make") is None or shutil.which("sh") is None,
    reason="needs GNU make and a POSIX shell (CI has both)",
)

STUB = r'''
import json, os, sys, gzip
args = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(args) + "\n")
joined = " ".join(args)
if "pg_dump" in joined:
    sys.stdout.buffer.write(gzip.compress(b"-- a dump\n-- PostgreSQL database dump complete\n"))
elif "information_schema.tables" in joined:
    print("0")
elif "'lots: '" in joined:
    print("lots: 7")
elif "-X" in args:
    sys.stdin.read()
'''


@pytest.fixture
def sandbox(tmp_path):
    """A copy of the Makefile and template, plus the compose stub."""
    shutil.copy(ROOT / "Makefile", tmp_path / "Makefile")
    shutil.copy(ROOT / ".env.template", tmp_path / ".env.template")
    (tmp_path / "stub.py").write_text(STUB, encoding="utf-8")
    return tmp_path


def _make(sandbox: Path, *args: str) -> tuple[subprocess.CompletedProcess, list[list[str]]]:
    log = sandbox / "calls.jsonl"
    log.unlink(missing_ok=True)
    env = {k: v for k, v in os.environ.items() if k not in ("ARGS", "MAKEFLAGS", "COMPOSE")}
    env["STUB_LOG"] = str(log)
    out = subprocess.run(
        ["make", "--no-print-directory", f"COMPOSE={sys.executable} {sandbox / 'stub.py'}", *args],
        cwd=sandbox, env=env, capture_output=True, text=True, timeout=120,
    )
    calls = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
    return out, calls


# ── E-7: free-form values are one argument, whatever they contain ────────────


@pytest.mark.parametrize(
    "question",
    [
        "what's my concentration risk?",
        'say "hello" and it\'s fine',
        # Shell syntax that must stay text. (A `$` is make's own syntax before
        # the shell ever sees it — `$$` on the command line — so it is not part
        # of what this recipe can or should change.)
        "`touch pwned` ; touch pwned2 && touch pwned3 | cat",
    ],
)
def test_ask_passes_the_question_as_one_literal_argument(sandbox, question):
    out, calls = _make(sandbox, "ask", f"ARGS={question}")
    assert out.returncode == 0, out.stderr
    assert calls[-1][-2:] == ["ask", question]
    assert not list(sandbox.glob("pwned*")), "ARGS was executed as shell, not passed as text"


def test_restore_accepts_a_path_with_an_apostrophe_and_spaces(sandbox):
    dump = sandbox / "it's a dump.sql.gz"
    dump.write_bytes(gzip.compress(b"CREATE TABLE lots (id int);\n"))
    out, calls = _make(sandbox, "restore", f"ARGS={dump}")
    assert out.returncode == 0, out.stdout + out.stderr
    assert any("--single-transaction" in c for c in calls), calls
    assert "restored" in out.stdout


def test_restore_still_refuses_a_missing_file_by_that_kind_of_name(sandbox):
    out, calls = _make(sandbox, "restore", f"ARGS={sandbox / 'no such dump.sql.gz'}")
    assert out.returncode != 0
    assert "no such file" in out.stdout
    assert calls == [], "a missing file must stop the recipe before compose is called"


def test_backup_writes_into_a_directory_with_an_apostrophe(sandbox):
    dest = sandbox / "o'brien backups"
    out, _ = _make(sandbox, "backup", f"ARGS={dest}")
    assert out.returncode == 0, out.stdout + out.stderr
    dumps = list(dest.glob("portfoliodb-*.sql.gz"))
    assert len(dumps) == 1
    assert gzip.decompress(dumps[0].read_bytes()).endswith(b"dump complete\n")


# ── the targets whose recipe changed in 1.8.0 ────────────────────────────────


def test_report_defaults_to_end_of_day_and_honours_args(sandbox):
    """report_portfolio_db.py requires --mode; the bare target used to fail."""
    _, calls = _make(sandbox, "report")
    assert calls[-1][-2:] == ["--mode", "eod"]
    _, calls = _make(sandbox, "report", "ARGS=--mode daily")
    assert calls[-1][-2:] == ["--mode", "daily"]


def test_make_test_builds_the_working_tree_and_never_reaches_the_database(sandbox):
    """Audit C-4: `make test` ran the published image, so it never saw an edit."""
    _, calls = _make(sandbox, "test")
    build, run = calls
    assert build[-2:] == ["build", "devtools"], "the tooling target (pip included) is the devtools service"
    assert "docker-compose.dev.yml" in build, "the build must use the dev overlay (build from source)"
    assert "docker-compose.dev.yml" in run, "the run must use the image just built"
    for call in (build, run):
        assert call[call.index("--profile") + 1] == "devtools", "devtools sits behind a profile"
    assert "run" in run and "devtools" in run
    assert "--no-deps" in run, "make test must not start (or need) the stack's postgres"
    assert run[run.index("-e") + 1] == "PORTFOLIODB_PASSWORD=", (
        "a blank password is what makes every database-backed test skip"
    )


def test_make_lock_freezes_the_dev_target_not_the_runtime(sandbox):
    """Audit O-2: the runtime image has no pip, so it cannot be frozen; the lock
    is regenerated in the devtools service (the Dockerfile's `dev` stage)."""
    (sandbox / "app").mkdir()
    (sandbox / "app" / "constraints.txt").write_text("# header\npip==1\n", encoding="utf-8")
    _, calls = _make(sandbox, "lock")
    assert calls, "make lock ran no compose command"
    for call in calls:
        assert "docker-compose.dev.yml" in call
        assert call[call.index("--profile") + 1] == "devtools"
        assert "devtools" in call[call.index("--profile") + 2:], call
        assert "dashboard" not in call, f"make lock still uses the runtime service: {call}"
    assert calls[0][-3:] == ["--build-arg", "PIP_LOCK=refresh", "devtools"]
    assert any(c[-4:] == ["pip", "freeze", "--all", "--exclude-editable"] for c in calls)


# ── init: the dashboard password ─────────────────────────────────────────────


def _env(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            out[k] = v
    return out


def test_init_generates_every_secret_including_the_dashboard_password(sandbox):
    out, _ = _make(sandbox, "init")
    assert out.returncode == 0, out.stderr
    env = _env(sandbox / ".env")
    assert env["POSTGRES_PASSWORD"]
    assert env["POSTGRES_PASSWORD"] == env["PORTFOLIODB_PASSWORD"]
    assert len(env["PORTFOLIODB_MCP_TOKEN"]) == 40
    assert len(env["PORTFOLIODB_DASHBOARD_PASSWORD"]) == 20
    assert oct((sandbox / ".env").stat().st_mode & 0o777) == "0o600"


def test_init_adds_the_dashboard_password_to_an_env_from_before_1_8(sandbox):
    """An existing .env has no line for the new key; sed alone would add nothing."""
    (sandbox / ".env").write_text(
        "POSTGRES_PASSWORD=same\nPORTFOLIODB_PASSWORD=same\nPORTFOLIODB_MCP_TOKEN=keepme", encoding="utf-8"
    )  # no trailing newline, on purpose
    out, _ = _make(sandbox, "init")
    assert out.returncode == 0, out.stderr
    env = _env(sandbox / ".env")
    assert env["PORTFOLIODB_MCP_TOKEN"] == "keepme"
    assert env["POSTGRES_PASSWORD"] == "same"
    assert len(env["PORTFOLIODB_DASHBOARD_PASSWORD"]) == 20


def test_init_is_idempotent(sandbox):
    _make(sandbox, "init")
    before = (sandbox / ".env").read_text(encoding="utf-8")
    out, _ = _make(sandbox, "init")
    assert (sandbox / ".env").read_text(encoding="utf-8") == before
    assert "Dashboard password already set" in out.stdout
