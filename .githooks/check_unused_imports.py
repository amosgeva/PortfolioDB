"""Flag module-level imports that are never used (the F401 case Codacy gates on).

Exists because a stray `import pytest` in a new test file has twice failed CI
after the work was already pushed. Codacy only reports *new* issues, so the
pre-existing unused imports on main are deliberately not the target here —
this checks the files you are actually committing.

Usage:
    python .githooks/check_unused_imports.py FILE [FILE ...]

Exits 1 if anything is flagged. No third-party dependencies: it runs from the
pre-commit hook, which must work on a bare checkout.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

# `from __future__ import ...` is a compiler directive, not a name to use.
_EXEMPT_MODULES = {"__future__"}


def imported_names(tree: ast.AST) -> dict[str, int]:
    """{bound name: line number} for every module-level import binding."""
    found: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                # `import a.b.c` binds `a`; `import a.b as ab` binds `ab`.
                found[alias.asname or alias.name.split(".")[0]] = node.lineno
        elif isinstance(node, ast.ImportFrom):
            if node.module in _EXEMPT_MODULES:
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue  # a star import binds names we cannot see
                found[alias.asname or alias.name] = node.lineno
    return found


def used_names(tree: ast.AST) -> set[str]:
    """Every identifier referenced anywhere, including through attribute chains."""
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            # `mod.sub.fn()` — walk down to the root Name so `mod` counts as used.
            root: ast.AST = node
            while isinstance(root, ast.Attribute):
                root = root.value
            if isinstance(root, ast.Name):
                used.add(root.id)
    return used


def check(path: Path) -> list[str]:
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as e:
        return [f"{path}: could not read ({e})"]
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return [f"{path}:{e.lineno}: syntax error: {e.msg}"]

    used = used_names(tree)
    problems: list[str] = []
    for name, line in sorted(imported_names(tree).items(), key=lambda kv: kv[1]):
        if name in used:
            continue
        # A name can also be referenced from a string annotation, __all__, or a
        # docstring. Counting raw occurrences is a crude but effective guard: the
        # import statement itself is one, so >1 means something else mentions it.
        if source.count(name) > 1:
            continue
        problems.append(f"{path}:{line}: '{name}' imported but unused (F401)")
    return problems


def main(argv: list[str]) -> int:
    paths = [Path(a) for a in argv if a.endswith(".py")]
    problems: list[str] = []
    for p in paths:
        if p.exists():
            problems.extend(check(p))

    for line in problems:
        print(line)
    if problems:
        print(f"\n{len(problems)} unused import(s). Remove them or use them.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
