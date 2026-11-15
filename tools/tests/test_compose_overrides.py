"""docker-compose.yml, and every override the docs show, must render as described.

Two generations of the same lesson live here.

First (audit F02, 2026-09-09): `docs/exposure.md` told an operator with an
untrusted LAN to bind the dashboard to loopback through a plain-list override,
and a plain list does not replace the base file's mapping. Compose identifies a
port entry by host IP, target, published port and protocol, so a loopback entry
sat *beside* the inherited wildcard one, and `ports: []` removed nothing. The
fix was the `!override` and `!reset` merge tags.

Second (audit A-1, 2026-11-14): the safe binding should not need an override at
all. Since 1.8.0 the base file binds every published port to 127.0.0.1 and the
LAN is the opt-in, so the tests now assert the default itself, and that each
documented override -- opening up as much as closing down -- renders exactly
the mappings its block spells out, nothing beside them.

Everything renders the base file plus the override with a synthetic `.env` and
asserts on the effective model rather than on the YAML text -- because the text
is exactly what looked right the first time. The same rendered model also
carries the container hardening and per-service environment scoping
(audit O-1/O-2/O-3), which are asserted at the end.

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
    "PORTFOLIODB_MCP_RO_PASSWORD=synthetic-ro\n"
    "PORTFOLIODB_DASHBOARD_PASSWORD=synthetic-dashboard\n"
    "ANTHROPIC_API_KEY=synthetic-llm-key\n"
    "OPENAI_API_KEY=synthetic-openai-key\n"
    "OPENROUTER_API_KEY=synthetic-openrouter-key\n"
    "LLM_API_KEY=synthetic-generic-llm-key\n"
    "FINANCIAL_DATASETS_API_KEY=synthetic-fd-key\n"
    # A key nobody has heard of yet: the collector's allowlist must not let
    # tomorrow's vendor key in either.
    "SOMEVENDOR_API_KEY=synthetic-future-key\n"
    "PORTFOLIODB_MARKET_START=13:30\n"
    "PORTFOLIODB_TZ=Asia/Jerusalem\n"
    "PORTFOLIODB_MCP_ALLOWED_HOSTS=nas.lan,192.168.1.20\n"
    "PGADMIN_EMAIL=nobody@example.invalid\n"
    "PGADMIN_PASSWORD=synthetic\n"
)

ALL_PROFILES = ("mcp", "tools")
APP_SERVICES = ("dashboard", "scheduler", "scheduler-llm", "mcp")
SCHEDULERS = ("scheduler", "scheduler-llm")

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


def _render(override_yaml: str, *, profile: str | tuple[str, ...] | None = None) -> dict:
    """Effective compose model for base + override, from a scratch project dir.

    A scratch directory rather than the repo root so the render never reads the
    operator's real `.env` or their own docker-compose.override.yml. A service
    behind a profile (mcp, pgadmin) is only rendered when that profile is named.
    """
    profiles = (profile,) if isinstance(profile, str) else (profile or ())
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
                *[a for p in profiles for a in ("--profile", p)],
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


_MAPPING = re.compile(r'^\s*-\s*"(?:(?P<ip>[0-9.]+):)?(?P<pub>\d+):(?P<target>\d+)"')


def _written_mappings(override_yaml: str) -> dict[str, set[tuple[str, str, int]]]:
    """What each service's `ports:` block literally says, as (ip, published, target).

    A mapping with no host IP is a wildcard bind, which Compose renders with no
    `host_ip` at all; it is normalised to 0.0.0.0 on both sides.
    """
    out: dict[str, set[tuple[str, str, int]]] = {}
    current = None
    in_ports = False
    for line in override_yaml.splitlines():
        m = re.match(r"^  (\w+):\s*$", line)
        if m:
            current, in_ports = m.group(1), False
            continue
        if current and re.match(r"^    ports:", line):
            in_ports = True
            out[current] = set()
            continue
        if in_ports:
            mm = _MAPPING.match(line)
            if mm:
                out[current].add((mm["ip"] or "0.0.0.0", mm["pub"], int(mm["target"])))
            elif re.match(r"^    \S", line):
                in_ports = False
    return out


def _rendered_mappings(model: dict, service: str) -> set[tuple[str, str, int]]:
    return {
        (e.get("host_ip") or "0.0.0.0", str(e.get("published")), int(e["target"]))
        for e in _ports(model, service)
    }


@requires_compose
def test_base_file_binds_every_published_port_to_loopback():
    """Audit A-1: the default install exposes nothing beyond the host itself.

    The writable dashboard and Postgres used to publish on every interface while
    the lower-risk MCP server and pgAdmin were already on loopback. Every
    service, every profile, no override: each published port is 127.0.0.1.
    """
    model = _render("services: {}\n", profile=ALL_PROFILES)
    published = {s: _ports(model, s) for s in model["services"]}
    assert published["dashboard"], "the dashboard stopped publishing"
    assert published["postgres"], "postgres stopped publishing"
    for service, entries in published.items():
        for entry in entries:
            assert entry.get("host_ip") == "127.0.0.1", (
                f"{service} publishes {entry} beyond loopback by default"
            )


@requires_compose
@pytest.mark.parametrize("label,block", _override_blocks_in_docs())
def test_documented_overrides_render_exactly_what_they_say(label, block):
    """Each block in the guide yields its own mappings and nothing beside them.

    This is the F02 lesson stated generally: a block that opens the dashboard to
    the LAN must not leave the loopback entry next to it, and a block that
    removes a mapping must remove it, not merge an empty list into it.
    """
    model = _render(block)
    written = _written_mappings(block)
    assert written, f"{label}: could not read any ports block from the example"
    for service, expected in written.items():
        assert _rendered_mappings(model, service) == expected, (
            f"{label}: {service} renders {_rendered_mappings(model, service)}, "
            f"the block says {expected}; the docs must use `ports: !override` / `!reset`"
        )


@requires_compose
def test_remove_postgres_publication_really_removes_it():
    """The guide's "remove the mapping" example must leave postgres unpublished."""
    blocks = [b for _, b in _override_blocks_in_docs() if "postgres" in b and "!reset" in b]
    assert blocks, "exposure.md lost its postgres `!reset` example"
    for block in blocks:
        model = _render(block)
        assert _ports(model, "postgres") == [], (
            f"postgres is still published after:\n{block}"
        )


@requires_compose
def test_the_guide_has_a_lan_opt_in_for_the_dashboard():
    """Opting in has to be documented, and has to really open the port."""
    lan = [b for _, b in _override_blocks_in_docs()
           if ("0.0.0.0", "8501", 8501) in _written_mappings(b).get("dashboard", set())]
    assert lan, "docs/exposure.md no longer shows how to opt in to LAN access"
    model = _render(lan[0])
    assert _rendered_mappings(model, "dashboard") == {("0.0.0.0", "8501", 8501)}


def _template_with_lan_block_enabled() -> str:
    """The template with its commented opt-in block uncommented, as a user would."""
    lines = OVERRIDE_TEMPLATE.read_text(encoding="utf-8").splitlines()
    start = next(i for i, l in enumerate(lines) if re.match(r"^  # dashboard:\s*$", l))
    enabled = lines[:start] + [re.sub(r"^  # ?", "  ", l) for l in lines[start:]]
    return "\n".join(enabled) + "\n"


@requires_compose
def test_override_template_as_copied_keeps_every_port_on_loopback():
    """docker-compose.override.yml.example is what a new operator copies, often
    for the ./data bind mount alone. Copying it must not widen exposure."""
    model = _render(OVERRIDE_TEMPLATE.read_text(encoding="utf-8"))
    for service in ("dashboard", "postgres"):
        entries = _ports(model, service)
        assert entries, f"the template stopped {service} publishing at all"
        assert all(e.get("host_ip") == "127.0.0.1" for e in entries), (
            f"copying the template leaves a non-loopback {service} mapping: {entries}"
        )
    assert model["services"]["postgres"]["volumes"][0]["source"].endswith("data")


@requires_compose
def test_override_template_lan_block_replaces_the_loopback_mapping():
    """Uncommented, the template's opt-in leaves exactly one wildcard mapping."""
    model = _render(_template_with_lan_block_enabled())
    assert _rendered_mappings(model, "dashboard") == {("0.0.0.0", "8501", 8501)}


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
    for service in ("dashboard", "scheduler", "scheduler-llm"):
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
        '      - "8501:8501"\n'
        "  postgres:\n"
        "    ports: []\n"
    )
    dashboard_ips = {e.get("host_ip") or "0.0.0.0" for e in _ports(model, "dashboard")}
    assert dashboard_ips == {"127.0.0.1", "0.0.0.0"}, (
        "a plain-list override no longer lands beside the base mapping; revisit the docs"
    )
    assert _ports(model, "postgres"), "`ports: []` now removes the mapping; revisit the docs"


# ── what each container may do (audit O-1, O-2) ──────────────────────────────


@pytest.fixture(scope="module")
def full_model() -> dict:
    if not _compose_available():
        pytest.skip("docker compose is not installed here")
    return _render("services: {}\n", profile=ALL_PROFILES)


@pytest.mark.parametrize("service", APP_SERVICES)
def test_app_services_run_with_no_capabilities_and_a_read_only_root(full_model, service):
    svc = full_model["services"][service]
    assert svc.get("cap_drop") == ["ALL"], service
    assert not svc.get("cap_add"), service
    assert "no-new-privileges:true" in (svc.get("security_opt") or []), service
    assert svc.get("read_only") is True, service
    tmpfs = svc.get("tmpfs") or []
    assert "/tmp" in tmpfs, service
    # pip --user (make test) and Streamlit/yfinance per-user state need a
    # writable home, owned by the image's appuser (uid 10001, app/Dockerfile).
    assert any(t.startswith("/home/appuser:") and "uid=10001" in t for t in tmpfs), service


def test_postgres_keeps_only_the_capabilities_its_entrypoint_needs(full_model):
    svc = full_model["services"]["postgres"]
    assert svc.get("cap_drop") == ["ALL"]
    assert sorted(svc.get("cap_add") or []) == ["CHOWN", "DAC_OVERRIDE", "FOWNER", "SETGID", "SETUID"]
    assert "no-new-privileges:true" in (svc.get("security_opt") or [])
    # Deliberately not read_only: `docker compose cp` (pdb.ps1 backup/restore)
    # refuses a read-only root filesystem. A change here must re-test that.
    assert not svc.get("read_only")


def test_pgadmin_runs_unprivileged_on_the_port_it_is_published_from(full_model):
    svc = full_model["services"]["pgadmin"]
    assert svc.get("cap_drop") == ["ALL"]
    assert "no-new-privileges:true" in (svc.get("security_opt") or [])
    env = svc["environment"]
    # no-new-privileges blocks both sudo (postfix) and the file capability that
    # lets it bind port 80, so both settings have to travel with the hardening.
    assert env.get("PGADMIN_DISABLE_POSTFIX") == "1"
    targets = {e["target"] for e in svc["ports"]}
    assert targets == {int(env["PGADMIN_LISTEN_PORT"])}


@pytest.mark.parametrize("service", ["postgres", "dashboard", "scheduler", "scheduler-llm", "mcp"])
def test_every_long_running_service_has_a_healthcheck(full_model, service):
    hc = full_model["services"][service].get("healthcheck") or {}
    assert hc.get("test"), f"{service} has no healthcheck"


def test_healthchecks_poll_the_endpoints_the_commands_actually_serve(full_model):
    """A healthcheck aimed at the wrong port is a container that is never healthy."""
    dash = full_model["services"]["dashboard"]
    port = re.search(r"--server\.port (\d+)", " ".join(dash["command"])).group(1)
    assert f"127.0.0.1:{port}/_stcore/health" in " ".join(dash["healthcheck"]["test"])

    for name in SCHEDULERS:
        sched = full_model["services"][name]
        cmd = sched["command"]
        listen = cmd[cmd.index("-prometheus-listen-address") + 1]
        assert listen.startswith("127.0.0.1:"), f"{name}: supercronic's metrics must stay on the container's loopback"
        assert f"http://{listen}/health" in " ".join(sched["healthcheck"]["test"]), name
        assert not _ports(full_model, name), f"{name} publishes nothing"


@pytest.mark.parametrize("service", ["postgres", "pgadmin"])
def test_third_party_images_are_pinned_by_digest(full_model, service):
    image = full_model["services"][service]["image"]
    assert re.fullmatch(r"[\w./-]+:[\w.-]+@sha256:[0-9a-f]{64}", image), (
        f"{service} image {image!r} must be tag@digest so a re-pushed tag cannot change it"
    )


# ── who holds which secret (audit O-3) ───────────────────────────────────────


def test_the_dashboard_password_reaches_the_dashboard_and_nothing_else(full_model):
    services = full_model["services"]
    assert _env(full_model, "dashboard").get("PORTFOLIODB_DASHBOARD_PASSWORD") == "synthetic-dashboard"
    assert _env(full_model, "scheduler-llm").get("PORTFOLIODB_DASHBOARD_PASSWORD") == ""
    assert "PORTFOLIODB_DASHBOARD_PASSWORD" not in _env(full_model, "scheduler")
    assert "PORTFOLIODB_DASHBOARD_PASSWORD" not in _env(full_model, "mcp")
    assert "PORTFOLIODB_DASHBOARD_PASSWORD" not in (services["pgadmin"].get("environment") or {})


@pytest.mark.parametrize("service", ["dashboard", "scheduler-llm"])
def test_other_processes_secrets_are_blanked_for_the_app(full_model, service):
    env = _env(full_model, service)
    for key in ("PORTFOLIODB_MCP_TOKEN", "PORTFOLIODB_MCP_RO_PASSWORD", "PGADMIN_PASSWORD"):
        assert env.get(key) == "", f"{service} still receives {key}"


def test_the_keyed_scheduler_keeps_the_keys_its_jobs_need(full_model):
    """The brief calls the LLM and the enrichment calls Financial Datasets."""
    env = _env(full_model, "scheduler-llm")
    assert env.get("ANTHROPIC_API_KEY") == "synthetic-llm-key"
    assert env.get("LLM_API_KEY") == "synthetic-generic-llm-key"
    assert env.get("FINANCIAL_DATASETS_API_KEY") == "synthetic-fd-key"
    assert env.get("PORTFOLIODB_PASSWORD") == "synthetic-not-a-secret"


# ── the collector's per-job secret boundary (audit O-3, re-score) ────────────

# Everything the quote collector may be handed: where the database is, its
# login, and the settings snapshot_prices.py and the modules it imports read
# from the environment when the settings table cannot answer. Anything else —
# above all an API key — stays out of the process that parses third-party pages.
COLLECTOR_ALLOWLIST = {
    "PORTFOLIODB_HOST", "PORTFOLIODB_PORT", "PORTFOLIODB_DB", "TZ",
    "PORTFOLIODB_USER", "PORTFOLIODB_PASSWORD",
    "PORTFOLIODB_TZ", "PORTFOLIODB_MARKET_START", "PORTFOLIODB_MARKET_END", "PORTFOLIODB_MARKET_WEEK",
    "PORTFOLIODB_MARKET_OVERVIEW", "PORTFOLIODB_COLLECTOR_DEADLINE_S", "PORTFOLIODB_QUOTE_MAX_AGE_MIN",
    "PORTFOLIODB_CONNECT_TIMEOUT",
}


def test_the_collector_is_handed_an_allowlist_not_the_env_file(full_model):
    svc = full_model["services"]["scheduler"]
    assert not svc.get("env_file"), "the collector must not receive .env wholesale"
    env = _env(full_model, "scheduler")
    assert set(env) <= COLLECTOR_ALLOWLIST, f"the collector receives {sorted(set(env) - COLLECTOR_ALLOWLIST)}"


def test_the_collector_holds_no_api_key_not_even_a_new_one(full_model):
    env = _env(full_model, "scheduler")
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY", "LLM_API_KEY",
                "FINANCIAL_DATASETS_API_KEY", "SOMEVENDOR_API_KEY", "PORTFOLIODB_MCP_TOKEN",
                "PORTFOLIODB_MCP_RO_PASSWORD", "PGADMIN_PASSWORD", "PORTFOLIODB_DASHBOARD_PASSWORD"):
        assert key not in env, f"the collector holds {key}"
    assert not [v for v in env.values() if str(v).startswith("synthetic-") and v != "synthetic-not-a-secret"], (
        "a synthetic secret reached the collector under some other name"
    )


def test_the_collector_still_gets_its_login_and_its_window(full_model):
    """The market window pinned in an operator's .env must still reach it."""
    env = _env(full_model, "scheduler")
    assert env["PORTFOLIODB_PASSWORD"] == "synthetic-not-a-secret"
    assert env["PORTFOLIODB_HOST"] == "postgres"
    assert env["PORTFOLIODB_MARKET_START"] == "13:30"
    assert env["PORTFOLIODB_TZ"] == "Asia/Jerusalem"


def test_each_scheduler_runs_its_own_crontab(full_model):
    assert full_model["services"]["scheduler"]["command"][-1] == "/app/docker/crontab"
    assert full_model["services"]["scheduler-llm"]["command"][-1] == "/app/docker/crontab-llm"


def _cron_commands(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [l.split(None, 5)[5] for l in lines if l.strip() and not l.lstrip().startswith("#")]


def test_the_crontabs_split_collection_from_keyed_jobs():
    """Collection in the keyless container, keyed calls in the other, and
    every job in exactly one of them."""
    collector = _cron_commands(ROOT / "docker" / "crontab")
    keyed = _cron_commands(ROOT / "docker" / "crontab-llm")
    assert collector, "docker/crontab has no jobs"
    assert keyed, "docker/crontab-llm has no jobs"
    assert all("snapshot_prices.py" in c for c in collector), collector
    assert not any("snapshot_prices.py" in c for c in keyed), keyed
    assert any("advisor.py brief" in c for c in keyed)
    assert any("fd_weekly_enrichment.py" in c for c in keyed)
    assert not any("advisor.py" in c or "fd_weekly_enrichment" in c for c in collector)


def test_the_image_ships_both_crontabs():
    text = (ROOT / "app" / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"^COPY docker/crontab docker/crontab-llm /app/docker/$", text, re.MULTILINE)


# ── the dev overlay: runtime for the stack, dev for the tooling (audit O-2) ──


@pytest.fixture(scope="module")
def dev_model() -> dict:
    if not _compose_available():
        pytest.skip("docker compose is not installed here")
    overlay = (ROOT / "docker-compose.dev.yml").read_text(encoding="utf-8")
    return _render(overlay, profile=("devtools", "mcp"))


def test_the_dev_overlay_builds_the_runtime_stage_for_every_service(dev_model):
    for name in APP_SERVICES:
        build = dev_model["services"][name].get("build") or {}
        assert build.get("target") == "runtime", f"{name} would run a stage with pip in it"


def test_the_devtools_service_is_the_dev_stage_hardened_and_keyless(dev_model):
    svc = dev_model["services"]["devtools"]
    assert svc["build"]["target"] == "dev"
    assert svc.get("profiles") == ["devtools"], "`make dev-up` must never start it"
    assert not svc.get("env_file"), "the test runner needs none of .env's secrets"
    assert svc.get("read_only") is True
    assert svc.get("cap_drop") == ["ALL"]
    assert "no-new-privileges:true" in (svc.get("security_opt") or [])
    assert svc["image"] != dev_model["services"]["dashboard"]["image"], (
        "the dev stage must not overwrite the image the stack runs"
    )


def test_the_mcp_server_gets_the_reporting_timezone(full_model):
    """Audit X-4: without it the MCP container bucketed days in UTC."""
    assert _env(full_model, "mcp").get("PORTFOLIODB_TZ") == "Asia/Jerusalem"


def test_the_mcp_server_gets_its_allowed_hosts(full_model):
    """Audit M-12: the Host guard refuses names it was not told about, so a
    LAN name set in .env must reach the mcp container, which is not handed the
    whole .env."""
    assert _env(full_model, "mcp").get("PORTFOLIODB_MCP_ALLOWED_HOSTS") == "nas.lan,192.168.1.20"
