"""The Makefile, docs/commands.md and pdb.ps1 must not drift apart.

There are now three descriptions of how to run this project:

  * `Makefile`         -- the runner on macOS and Linux
  * `docs/commands.md` -- every target beside the `docker compose` line it runs,
                          which is what a Windows reader follows
  * `pdb.ps1`          -- the Windows runner, deliberately covering only the
                          three targets that carry real logic

Three descriptions of one thing is a drift machine, and the drift is silent:
adding a Makefile target leaves the docs quietly incomplete, and nothing on a
Linux CI runner notices. That is the whole risk the Windows work took on, so it
gets a test rather than a good intention.

These are pure text assertions over tracked files. No Docker, no network, no
Postgres -- they run in the fast suite on every pull request.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = ROOT / "Makefile"
COMMANDS_DOC = ROOT / "docs" / "commands.md"
WRAPPER = ROOT / "pdb.ps1"

# Implemented in pdb.ps1 because they carry real logic rather than wrapping a
# single command. Everything else is documented, not wrapped -- see the module
# docstring in pdb.ps1 for why.
WRAPPED = {"init", "backup", "restore"}

# `help` prints the Makefile's own target list, so it has nothing to translate.
NOT_TRANSLATABLE = {"help"}


def _phony_targets() -> set[str]:
    """Every target the Makefile declares in .PHONY, across continuations."""
    text = MAKEFILE.read_text(encoding="utf-8")
    match = re.search(r"^\.PHONY:((?:[^\n\\]*\\\n)*[^\n]*)", text, re.MULTILINE)
    assert match, "Makefile has no .PHONY line to read targets from"
    return set(match.group(1).replace("\\", " ").split())


def test_phony_matches_the_real_rules():
    """A target missing from .PHONY would silently escape every check below."""
    text = MAKEFILE.read_text(encoding="utf-8")
    rules = set(re.findall(r"^([a-z][a-z-]*):(?!=)", text, re.MULTILINE))
    declared = _phony_targets()
    assert rules - declared == set(), (
        f"these Makefile rules are missing from .PHONY: {sorted(rules - declared)}"
    )


@pytest.mark.parametrize("target", sorted(_phony_targets() - NOT_TRANSLATABLE))
def test_every_target_is_documented_or_wrapped(target):
    """No target may exist without a non-make route to the same behaviour.

    Either docs/commands.md gives the compose line, or pdb.ps1 implements it.
    A new target that skips both is exactly the drift this file exists to catch.
    """
    doc = COMMANDS_DOC.read_text(encoding="utf-8")
    documented = re.search(rf"^\|\s*`{re.escape(target)}[ `]", doc, re.MULTILINE) or re.search(
        rf"^##\s+{re.escape(target)}\s*$", doc, re.MULTILINE
    )
    assert documented or target in WRAPPED, (
        f"Makefile target '{target}' appears in neither docs/commands.md nor pdb.ps1. "
        "Add the docker compose equivalent to docs/commands.md."
    )


@pytest.mark.parametrize("target", sorted(WRAPPED))
def test_wrapped_targets_are_actually_in_the_wrapper(target):
    """WRAPPED is a claim about pdb.ps1; keep it honest."""
    script = WRAPPER.read_text(encoding="utf-8")
    assert re.search(rf"^\s*'{re.escape(target)}'\s*\{{", script, re.MULTILINE), (
        f"pdb.ps1 has no dispatch branch for '{target}'"
    )


def test_wrapper_is_pure_ascii():
    """Windows PowerShell 5.1 reads a .ps1 as the system codepage without a BOM.

    An em-dash in a Write-Host string therefore reaches the console as mojibake
    (measured: '-' became 'a<euro>"'). Rather than add a BOM -- which still
    leaves the console's own codepage free to mangle it -- the script is ASCII
    only, and this keeps it that way.
    """
    raw = WRAPPER.read_bytes()
    offenders = [(i, b) for i, b in enumerate(raw) if b > 0x7F]
    assert not offenders, (
        f"pdb.ps1 must stay ASCII; first non-ASCII byte at offset {offenders[0][0]}. "
        "Use '-' rather than an em-dash."
    )


@pytest.mark.parametrize(
    "pattern,what",
    [
        (r"\?\?", "the null-coalescing operator (??)"),
        (r"\?\.", "null-conditional access (?.)"),
        (r"\$\w+\s*\?\s*[\"'$]", "the ternary operator (? :)"),
    ],
)
def test_wrapper_avoids_powershell_7_only_syntax(pattern, what):
    """A fresh Windows box has PowerShell 5.1, and 5.1 cannot parse these.

    CI runs the Pester suite under both hosts, but a parse error in a branch no
    test reaches would still ship. This is cheap and catches it at the source.
    """
    script = WRAPPER.read_text(encoding="utf-8")
    # Strip comments and the help block: prose may legitimately mention them,
    # and the script's own header does exactly that.
    code = re.sub(r"<#.*?#>", "", script, flags=re.DOTALL)
    code = "\n".join(line.split("#", 1)[0] for line in code.splitlines())
    assert not re.search(pattern, code), (
        f"pdb.ps1 uses {what}, which Windows PowerShell 5.1 cannot parse"
    )


def test_backup_compresses_inside_the_container():
    """The one bug in this area that data loss depends on.

    `pg_dump | gzip > file` is correct on POSIX and wrong on Windows: there is
    no host gzip, and 5.1 re-encodes a native command's binary output on
    redirection. Measured on this project, the same dump came out 5.6 MB and
    unreadable against 2.9 MB and valid -- and nothing warns you until the day
    you restore. So the wrapper must gzip inside the container and move the
    finished file with `docker compose cp`.
    """
    script = WRAPPER.read_text(encoding="utf-8")
    assert "gzip -c > $tmp" in script, "backup must compress inside the container"
    assert re.search(r"'cp',\s*\"postgres:\$tmp\"", script), (
        "backup must move the finished file out with `docker compose cp`"
    )
    # A host-side redirect of the dump is the defect itself.
    assert not re.search(r"pg_dump[^\n]*\|\s*gzip[^\n]*'\s*\)?\s*>", script), (
        "backup must not redirect the dump through the host shell"
    )


def test_restore_keeps_the_empty_database_guard():
    """Without this, restore is a data-loss bug rather than a recovery tool."""
    script = WRAPPER.read_text(encoding="utf-8")
    assert "information_schema.tables" in script, (
        "restore must count existing tables before writing"
    )
    assert "Refusing to restore" in script, "restore must refuse a non-empty database"
