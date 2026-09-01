"""The daily briefing's best-effort price refresh.

`report_portfolio_db` used to shell out to a hardcoded
`C:\\Windows\\...\\powershell.exe` running
`C:\\Install\\PortfolioDB\\run_snapshot.ps1` when the latest snapshot looked
thin. That launcher is gitignored — host-specific PowerShell wiring — so it
exists on exactly one machine. Anywhere else the call failed into a bare
`except: pass`, and the briefing carried on with a stale snapshot in silence.

These pin the properties that keep it portable and honest: the collector is
addressed relatively and run with the interpreter already in hand, the market
window is left to the collector, and a failure is reported rather than
swallowed.
"""

import os
import sys
from subprocess import TimeoutExpired  # nosemgrep
from types import SimpleNamespace

import pytest


def completed(returncode=0, stdout="", stderr=""):
    """Stand-in for a finished subprocess.

    collect_fresh_prices reads exactly three attributes — returncode, stdout,
    stderr — so a namespace is a faithful double and, unlike a real
    CompletedProcess, does not couple these tests to subprocess internals.
    (It also stops an audit rule reading `subprocess.CompletedProcess(...)` in
    a test file as though it were launching something.)
    """
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import report_portfolio_db as rp


def test_the_collector_script_actually_exists():
    """A relative path is only an improvement if it resolves."""
    assert rp.SNAPSHOT_SCRIPT.is_file(), f"{rp.SNAPSHOT_SCRIPT} is not on disk"


def test_the_script_path_is_derived_from_this_module_not_written_down():
    """The portability property, stated precisely.

    SNAPSHOT_SCRIPT is absolute once resolved — that is fine and unavoidable.
    What matters is where the absolute part comes from: the module's own
    location, so it follows a clone, a container, or a rename. A path spelled
    out in the source only works on the machine it was written on.
    """
    from pathlib import Path

    assert rp.SNAPSHOT_SCRIPT.parent == Path(rp.__file__).resolve().parent
    assert rp.SNAPSHOT_SCRIPT.name == "snapshot_prices.py"


def test_the_command_runs_python_not_a_shell_launcher(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        seen["kw"] = kw
        return completed()

    monkeypatch.setattr(rp.subprocess, "run", fake_run)
    rp.collect_fresh_prices()

    cmd = seen["cmd"]
    assert cmd[0] == sys.executable, "must reuse the running interpreter"
    assert cmd[1] == str(rp.SNAPSHOT_SCRIPT)
    joined = " ".join(cmd).lower()
    assert "powershell" not in joined
    assert ".ps1" not in joined


def test_the_market_window_is_left_to_the_collector(monkeypatch):
    """No --ignore-window.

    The collector refuses outside the configured window, and that refusal is
    meant to be one rule every caller obeys. A briefing run at midnight should
    report stale prices rather than manufacture a snapshot.
    """
    seen = {}
    monkeypatch.setattr(rp.subprocess, "run",
                        lambda cmd, **kw: seen.setdefault("cmd", cmd) or
                        completed())
    rp.collect_fresh_prices()
    assert "--ignore-window" not in seen["cmd"]


def test_a_bounded_wait(monkeypatch):
    """A hung collector must not hang the report."""
    seen = {}
    monkeypatch.setattr(rp.subprocess, "run",
                        lambda cmd, **kw: seen.update(kw) or
                        completed())
    rp.collect_fresh_prices()
    assert seen.get("timeout"), "no timeout — a stuck collector would block the briefing"
    assert seen.get("check") is False, "a failed refresh must not abort the report"


@pytest.mark.parametrize(
    "outcome",
    [
        "nonzero",
        "raises",
        "timeout",
    ],
)
def test_failures_are_reported_and_survivable(monkeypatch, capsys, outcome):
    """Degraded, not silent, and never fatal — the old code was silent."""
    def fake_run(cmd, **kw):
        if outcome == "nonzero":
            return completed(returncode=2, stderr="collector said no")
        if outcome == "timeout":
            # The real exception type on purpose: if collect_fresh_prices ever
            # narrows its `except Exception`, this case starts failing.
            raise TimeoutExpired(cmd, 180)  # nosemgrep
        raise OSError("no interpreter")

    monkeypatch.setattr(rp.subprocess, "run", fake_run)
    rp.collect_fresh_prices()          # must not raise

    printed = capsys.readouterr().out
    assert printed.strip(), "a failed refresh produced no output at all"
    assert "existing snapshot" in printed


def test_success_says_nothing(monkeypatch, capsys):
    """Only failures are worth a line in a report someone reads daily."""
    monkeypatch.setattr(rp.subprocess, "run",
                        lambda cmd, **kw: completed(stdout="ok"))
    rp.collect_fresh_prices()
    assert capsys.readouterr().out == ""


def test_threshold_is_a_named_constant():
    assert isinstance(rp.MIN_PRICED_SYMBOLS, int)
    assert rp.MIN_PRICED_SYMBOLS > 0
