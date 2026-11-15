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

Most of these are pure text assertions over tracked files. The one that checks
the documented commands *match* the recipes (audit K-5 -- before it, a row only
had to name the target) asks GNU make itself to expand each recipe with
`make -n`, because reimplementing make's variable expansion in a test is how
the test and the Makefile would start to disagree. It needs `make`, which every
CI runner has; it skips where there is none. No Docker, no network, no Postgres.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
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


# ── the documented command must be the command (audit K-5) ───────────────────

_ROW = re.compile(r"^\|\s*`(?P<make>[^`]+)`\s*\|(?P<runs>.*)\|\s*$", re.MULTILINE)
_MAKE_CELL = re.compile(
    r'^(?P<target>[a-z][a-z-]*)(?:\s+ARGS=(?:"(?P<quoted>[^"]*)"|(?P<word>\S+)))?\s*$'
)
_SPAN = re.compile(r"`(docker compose [^`]+)`")
_SEPARATORS = set(";&|<>()")


def _doc_rows() -> list[tuple[str, str | None, str]]:
    """(target, ARGS or None, runs-cell) for every two-column table row."""
    rows = []
    for m in _ROW.finditer(COMMANDS_DOC.read_text(encoding="utf-8")):
        cell = _MAKE_CELL.match(m["make"].strip())
        if not cell or cell["target"] == "make":  # the "| `make` | Runs |" header
            continue
        args = cell["quoted"] if cell["quoted"] is not None else cell["word"]
        rows.append((cell["target"], args, m["runs"]))
    assert rows, "docs/commands.md has no command tables left to check"
    return rows


def _compose_invocations(shell_text: str) -> list[list[str]]:
    """Every `docker compose ...` simple command in a piece of shell, as argv.

    Tokenised the way sh would split words (quotes removed, so `"…"` and `'…'`
    compare equal, as they do to the program receiving them). An invocation
    runs until the first operator: `;`, `&&`, `|`, a redirection. A recipe's
    backslash-newline continuations are joined first, and whitespace inside each
    word is collapsed so a multi-line `sh -c` script compares by content.
    """
    text = re.sub(r"\\\n[ \t]*", " ", shell_text)
    tokens: list[str] = []
    for line in text.splitlines():  # one recipe line, one shell command list
        lex = shlex.shlex(line, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        tokens += [" ".join(t.split()) for t in lex] + [";"]
    out, i = [], 0
    while i < len(tokens) - 1:
        if tokens[i] == "docker" and tokens[i + 1] == "compose":
            j = i
            while j < len(tokens) and not (tokens[j] and set(tokens[j]) <= _SEPARATORS):
                j += 1
            out.append(tokens[i:j])
            i = j
        else:
            i += 1
    return out


def _make_dry_run(target: str, args: str | None) -> str:
    env = {k: v for k, v in os.environ.items() if k not in ("ARGS", "PASSWORD", "COMPOSE", "MAKEFLAGS")}
    cmd = ["make", "--no-print-directory", "-n", target, "COMPOSE=docker compose"]
    if args is not None:
        cmd.append(f"ARGS={args}")
    out = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True,
                         encoding="utf-8", timeout=60)
    assert out.returncode == 0, f"make -n {target} failed:\n{out.stderr}"
    return out.stdout


_HAVE_MAKE = shutil.which("make") is not None


@pytest.mark.skipif(not _HAVE_MAKE, reason="GNU make is not installed here (CI has it)")
@pytest.mark.parametrize(
    "target,args,runs",
    _doc_rows(),
    ids=lambda v: v if isinstance(v, str) and len(v) < 40 else None,
)
def test_documented_commands_match_the_recipes(target, args, runs):
    """Each row's `docker compose` commands are exactly what `make` runs.

    Same commands, same order, same arguments. A recipe that grows a flag, or a
    row nobody updated, fails here instead of sending a Windows reader to run
    something the Makefile no longer does.
    """
    documented = [inv for span in _SPAN.findall(runs) for inv in _compose_invocations(span)]
    recipe = _compose_invocations(_make_dry_run(target, args))
    assert recipe, f"make -n {target} runs no docker compose command; is the row in the right table?"
    assert documented == recipe, (
        f"docs/commands.md row for `{target}` does not match the Makefile.\n"
        f"  documented: {[' '.join(map(shlex.quote, d)) for d in documented]}\n"
        f"  make runs:  {[' '.join(map(shlex.quote, r)) for r in recipe]}"
    )


def test_compose_invocation_parser_reads_what_sh_would():
    """The comparison above is only as good as this split; pin its behaviour."""
    got = _compose_invocations(
        "PORTFOLIODB_VERSION=$(git rev-parse --short HEAD 2>/dev/null || echo dev) "
        "docker compose -f a.yml build\n"
        "{ sed -n x f; docker compose run --rm -T d pip freeze; } > out && mv out f\n"
        "docker compose run --rm d sh -c '\\\n  echo  hi && \\\n  cd /app'\n"
        "docker compose run --rm d python ask 'what'\\''s up'\n"
    )
    assert got == [
        ["docker", "compose", "-f", "a.yml", "build"],
        ["docker", "compose", "run", "--rm", "-T", "d", "pip", "freeze"],
        ["docker", "compose", "run", "--rm", "d", "sh", "-c", "echo hi && cd /app"],
        ["docker", "compose", "run", "--rm", "d", "python", "ask", "what's up"],
    ]
    # Quote style is not a difference; an extra argument is.
    assert _compose_invocations('docker compose run x "a b"') == _compose_invocations("docker compose run x 'a b'")
    assert _compose_invocations("docker compose run x a b") != _compose_invocations("docker compose run x 'a b'")


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


def test_wrapper_passes_no_double_quotes_to_native_commands():
    """Windows PowerShell 5.1 hands a native command an argument's embedded
    double quotes *unescaped*, so a `bash -c` string containing `"` reaches
    bash mangled and dies with a syntax error (exit 2) -- while the same line
    works on PowerShell 7. Found the day backup verification landed: the
    `test "$(...)"` idiom passed every check on 7 and broke every backup on
    the host that ships with Windows. The command strings stay quote-free;
    `grep -c` output is one word and needs none.
    """
    script = WRAPPER.read_text(encoding="utf-8")
    assert '`"' not in script, (
        "pdb.ps1 embeds a double quote in a string that reaches a native command; "
        "5.1 will not escape it"
    )


def test_backup_verifies_before_it_keeps_anything():
    """A pipeline's exit status is its last command's, so a dead pg_dump still
    fed gzip a valid archive of nothing. The wrapper must run the dump under
    pipefail, prove the archive decompresses, look for pg_dump's completion
    marker, and only then rename the .part file.
    """
    script = WRAPPER.read_text(encoding="utf-8")
    assert "'bash', '-o', 'pipefail'" in script, "backup must run the dump under pipefail"
    assert "gzip -t $tmp" in script, "backup must test-decompress the archive in the container"
    assert "PostgreSQL database dump complete" in script, "backup must look for pg_dump's completion marker"
    assert '"$outFile.part"' in script and "Move-Item -LiteralPath $partFile" in script, (
        "backup must write to a .part name and rename only after verification"
    )


def test_restore_stops_on_the_first_error_and_rolls_back():
    """psql's defaults skip a failed statement and exit 0, so a half-loaded
    database used to print "restored". The first error must abort, the whole
    load must roll back, and a corrupt archive must fail before psql sees it.
    """
    script = WRAPPER.read_text(encoding="utf-8")
    assert "-v ON_ERROR_STOP=1" in script, "restore must stop psql on the first error"
    assert "--single-transaction" in script, "restore must roll back a failed load"
    assert re.search(r"'gzip',\s*'-t',\s*\$tmp", script), "restore must gzip -t the archive first"


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
