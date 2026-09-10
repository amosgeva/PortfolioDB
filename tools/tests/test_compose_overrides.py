"""The exposure guide's compose overrides must do what the prose says.

`docs/exposure.md` tells an operator with an untrusted LAN to bind the dashboard
to loopback, and an operator with no host tools to stop publishing Postgres,
each through `docker-compose.override.yml`. Both examples used to be plain
lists, and a plain list does not replace the base file's mapping: Compose
identifies a port entry by host IP, target, published port and protocol, so a
loopback entry sits *beside* the inherited wildcard one, and `ports: []` removes
nothing. The rendered model kept `0.0.0.0:8501` and `0.0.0.0:54320` while the
guide called the result "localhost only" (audit finding F02, 2026-09-09).

The fix is the `!override` and `!reset` merge tags. These tests render the base
file plus every override block the docs and the override template show, with a
synthetic `.env`, and assert on the effective model rather than on the YAML
text -- because the text is exactly what looked right the first time.

They shell out to `docker compose config`, which is on every GitHub runner and
needs no daemon, and skip when it is absent. Nothing here starts a container.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BASE_COMPOSE = ROOT / "docker-compose.yml"
EXPOSURE_DOC = ROOT / "docs" / "exposure.md"
OVERRIDE_TEMPLATE = ROOT / "docker-compose.override.yml.example"

# Values compose needs to interpolate; none of them is real and none of them is
# printed -- `config` output is parsed, never echoed.
SYNTHETIC_ENV = (
    "POSTGRES_PASSWORD=synthetic-not-a-secret\n"
    "PORTFOLIODB_MCP_TOKEN=synthetic-not-a-token\n"
    "PGADMIN_EMAIL=nobody@example.invalid\n"
    "PGADMIN_PASSWORD=synthetic\n"
)

_FENCE = re.compile(r"```yaml\n(.*?)```", re.DOTALL)


def _compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        out = subprocess.run(
            ["docker", "compose", "version"], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return out.returncode == 0


requires_compose = pytest.mark.skipif(
    not _compose_available(), reason="docker compose is not installed here"
)


def _override_blocks_in_docs() -> list[tuple[str, str]]:
    """Every ```yaml block in the exposure guide that changes a `ports:` key."""
    text = EXPOSURE_DOC.read_text(encoding="utf-8")
    blocks = []
    for i, m in enumerate(_FENCE.finditer(text)):
        body = m.group(1)
        if "services:" in body and "ports:" in body:
            blocks.append((f"exposure.md block {i}", body))
    assert blocks, "docs/exposure.md no longer shows any ports override to check"
    return blocks


def _render(override_yaml: str, *, profile: str | None = None) -> dict:
    """Effective compose model for base + override, from a scratch project dir.

    A scratch directory rather than the repo root so the render never reads the
    operator's real `.env` or their own docker-compose.override.yml. A service
    behind a profile (mcp) is only rendered when that profile is named.
    """
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        shutil.copy(BASE_COMPOSE, d / "docker-compose.yml")
        (d / "override.yml").write_text(override_yaml, encoding="utf-8")
        (d / ".env").write_text(SYNTHETIC_ENV, encoding="utf-8")
        (d / "philosophy.md").write_text("", encoding="utf-8")
        out = subprocess.run(
            [
                "docker", "compose",
                "--project-name", "pdbrendercheck",
                *(["--profile", profile] if profile else []),
                "-f", "docker-compose.yml",
                "-f", "override.yml",
                "config", "--format", "json",
            ],
            cwd=d,
            capture_output=True,
            text=True,
            timeout=120,
        )
    assert out.returncode == 0, f"docker compose config failed:\n{out.stderr}"
    return json.loads(out.stdout)


def _ports(model: dict, service: str) -> list[dict]:
    return model["services"][service].get("ports") or []


def _env(model: dict, service: str) -> dict:
    return model["services"][service].get("environment") or {}


def _touched_services(override_yaml: str) -> set[str]:
    """Services whose `ports:` the override block sets."""
    names = set()
    current = None
    for line in override_yaml.splitlines():
        m = re.match(r"^  (\w+):\s*$", line)
        if m:
            current = m.group(1)
        elif current and re.match(r"^    ports:", line):
            names.add(current)
    return names


@requires_compose
@pytest.mark.parametrize("label,block", _override_blocks_in_docs())
def test_documented_overrides_leave_no_wildcard_binding(label, block):
    """Every port the override touches ends up loopback-only or gone."""
    model = _render(block)
    for service in _touched_services(block):
        for entry in _ports(model, service):
            assert entry.get("host_ip") == "127.0.0.1", (
                f"{label}: {service} still publishes {entry} after the override; "
                "the docs must use `ports: !override` / `ports: !reset`"
            )


@requires_compose
def test_remove_postgres_publication_really_removes_it():
    """The guide's "remove the mapping" example must leave postgres unpublished."""
    blocks = [b for _, b in _override_blocks_in_docs() if "postgres" in b and "dashboard" not in b]
    assert blocks, "exposure.md lost its postgres-only override example"
    for block in blocks:
        model = _render(block)
        assert _ports(model, "postgres") == [], (
            f"postgres is still published after:\n{block}"
        )


@requires_compose
def test_override_template_binds_dashboard_to_loopback_only():
    """docker-compose.override.yml.example is what a new operator copies."""
    model = _render(OVERRIDE_TEMPLATE.read_text(encoding="utf-8"))
    entries = _ports(model, "dashboard")
    assert entries, "the template no longer publishes the dashboard at all"
    assert all(e.get("host_ip") == "127.0.0.1" for e in entries), (
        f"the template leaves a non-loopback dashboard mapping: {entries}"
    )


# ── what the mcp container is handed (re-audit N03) ──────────────────────────


def _render_mcp(override_yaml: str = "services: {}\n") -> dict:
    return _render(override_yaml, profile="mcp")


@requires_compose
def test_the_mcp_service_is_not_handed_the_write_password():
    """It brings its own read-only role; the application's login is not its business."""
    model = _render_mcp()
    mcp = _env(model, "mcp")
    assert "PORTFOLIODB_PASSWORD" not in mcp, "the mcp container holds the read-write password again"
    assert "PORTFOLIODB_USER" not in mcp
    # It still knows where the database is, and the services that write still log in.
    assert mcp["PORTFOLIODB_HOST"] == "postgres" and mcp["PORTFOLIODB_DB"] == "portfoliodb"
    assert "PORTFOLIODB_MCP_RO_USER" in mcp and "PORTFOLIODB_MCP_RO_PASSWORD" in mcp
    for service in ("dashboard", "scheduler"):
        env = _env(model, service)
        assert env.get("PORTFOLIODB_PASSWORD") == "synthetic-not-a-secret", service
        assert env.get("PORTFOLIODB_HOST") == "postgres", service


def _fallback_block_in_docs() -> str:
    text = EXPOSURE_DOC.read_text(encoding="utf-8")
    blocks = [m.group(1) for m in _FENCE.finditer(text)
              if "mcp:" in m.group(1) and "PORTFOLIODB_PASSWORD" in m.group(1)]
    assert len(blocks) == 1, "exposure.md should show exactly one fallback override block"
    return blocks[0]


@requires_compose
def test_the_documented_fallback_override_supplies_the_password():
    """The block the guide shows for the opt-out must actually hand the password over."""
    model = _render_mcp(_fallback_block_in_docs())
    mcp = _env(model, "mcp")
    assert mcp.get("PORTFOLIODB_PASSWORD") == "synthetic-not-a-secret"
    assert mcp.get("PORTFOLIODB_MCP_ALLOW_RW_FALLBACK") == "1"


@requires_compose
def test_a_plain_list_override_does_not_replace_the_mapping():
    """Documents *why* the tags are needed, and keeps the guard honest.

    If Compose ever changes its merge rule so that a plain list replaces the
    inherited entries, this test fails, and the `!override` advice in the docs
    can be reconsidered rather than carried forward as folklore.
    """
    model = _render(
        "services:\n"
        "  dashboard:\n"
        "    ports:\n"
        '      - "127.0.0.1:8501:8501"\n'
        "  postgres:\n"
        "    ports: []\n"
    )
    dashboard_ips = {e.get("host_ip") for e in _ports(model, "dashboard")}
    assert None in dashboard_ips or "0.0.0.0" in dashboard_ips, (
        "a plain-list override now replaces the base mapping; revisit the docs"
    )
    assert _ports(model, "postgres"), "`ports: []` now removes the mapping; revisit the docs"
