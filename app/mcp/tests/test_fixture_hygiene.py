"""Guards on the `fake_db` fixture itself.

The fixture patches `get_conn` into every module that holds its own reference.
Two things can go wrong, and both are silent:

1. A module first imported *after* `deps.get_conn` is patched binds the fake at
   import time. monkeypatch then records the fake as that module's original and
   restores it on teardown, so the fake outlives the test and every later test
   reading the real database sees a FakeConn. That bit test_reconciliation.py,
   and it looked like flakiness because which modules are already imported
   depends on which tests ran first.
2. A new module starts importing `get_conn` and nobody adds it to the patch
   list, so it quietly talks to the real database inside a stubbed test.

The patch list is discovered from the source here rather than hardcoded, so
adding a call site fails this test instead of going unnoticed.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

_MCP_ROOT = Path(__file__).resolve().parent.parent


def _call_site_modules() -> list[str]:
    """Every non-test module under app/mcp that imports `get_conn` by name."""
    out = []
    for path in sorted(_MCP_ROOT.rglob("*.py")):
        if "tests" in path.parts or path.name == "deps.py":
            continue
        if "import get_conn" not in path.read_text(encoding="utf-8"):
            continue
        rel = path.relative_to(_MCP_ROOT.parent.parent)
        out.append(".".join(rel.with_suffix("").parts))
    return out


def _binding(module_name: str):
    return getattr(importlib.import_module(module_name), "get_conn", None)


def test_call_sites_were_found():
    """A discovery bug would make the other two tests vacuously pass."""
    found = _call_site_modules()
    assert len(found) >= 12, found
    assert "app.mcp.services.income" in found


def test_fake_db_patches_every_call_site(env_token, fake_db):
    unpatched = [
        name for name in _call_site_modules()
        if "fake_get_conn" not in getattr(_binding(name), "__qualname__", "")
    ]
    assert not unpatched, (
        "these modules import get_conn but fake_db does not patch them, so a "
        f"stubbed test would hit the real database through them: {unpatched}"
    )


def test_fake_db_restores_every_call_site():
    """Runs after the test above — definition order is execution order."""
    from app.mcp import deps

    leaked = [
        name for name in _call_site_modules()
        if _binding(name) is not deps.get_conn
    ]
    assert not leaked, (
        "fake_db left a fake connection bound in these modules; a later test "
        f"reading the real database will get FakeConn instead: {leaked}"
    )


@pytest.mark.parametrize("name", _call_site_modules())
def test_call_site_binds_the_real_get_conn_by_default(name):
    """Parametrised so a leak names the offending module directly."""
    from app.mcp import deps

    assert _binding(name) is deps.get_conn
