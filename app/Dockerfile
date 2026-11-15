# Shared PortfolioDB application image ("portfoliodb-app").
#
# One image, several commands — the codebase is a single Python tree with one
# requirements.txt, so every service in docker-compose.yml builds from here and
# overrides `command:`:
#
#   dashboard  streamlit run app/streamlit_app.py
#   mcp        uvicorn app.mcp.server:asgi
#   scheduler  /usr/local/bin/supercronic /app/docker/crontab  (absolute path!)
#   CLIs       docker compose run --rm dashboard python app/add_lot.py ...
#
# Three stages (audit O-2): `base` installs everything; `dev` is base plus the
# unprivileged user, and keeps pip — `make lock`, `make test` and CI's lock check
# build it, because freezing a lock and installing a test runner need an
# installer; `runtime`, the LAST stage and so the default target, is the same
# filesystem with pip and ensurepip's bundled pip wheel removed. Everything that
# is published or run by the stack (publish.yml, security-scan.yml, the dev
# overlay's services, a bare `docker build`) is `runtime`: a running service has
# no package installer and no installer-vendored dependencies in it.

# Pinned by digest, not by tag. `python:3.14-slim` is a moving target — a
# rebuild of the same commit a month later got a different Debian snapshot and
# a different patch release, so "the image CI tested" and "the image a user
# pulled" were only nominally the same. The digest below is the multi-arch
# index; Dependabot (.github/dependabot.yml) proposes the bump when the tag
# moves, and the weekly scan (security-scan.yml) says when a bump is overdue.
FROM python:3.14-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f AS base

# supercronic: cron built for containers — logs every job to stdout, needs no
# root, and refuses to start on an invalid crontab instead of silently doing
# nothing (the failure mode that makes plain cron in Docker so unpleasant).
#
# Pinned by version and verified by SHA-256 per architecture, so a rebuild can
# never silently swap the binary. TARGETARCH is supplied by BuildKit, which
# keeps this working for the multi-arch build in Phase 6.
#
# The curl below is -L, so it follows redirects. `--proto '=https'` and
# `--proto-redir '=https'` keep every hop on TLS: without the second one a
# redirect to http:// is followed happily, and the download becomes tamperable
# by anything on the path. The sha256sum check that follows would catch a
# swapped binary, but only after it had been written to disk.
# v0.2.51 (2026-10-21) is built with Go 1.27.3. Go 1.27.2 (v0.2.50) carried a
# HIGH crypto/x509 advisory, and earlier Go releases others in net/http, http2
# and crypto/tls, all reported by the weekly Trivy scan against the binary.
# Checksums computed from the release assets on 2026-11-14; each matches the
# SHA-1 in the release notes and the sha256 GitHub's API records.
ARG SUPERCRONIC_VERSION=v0.2.51
ARG TARGETARCH
RUN set -eu; \
    case "${TARGETARCH:-amd64}" in \
      amd64) sha=cc232108374e8e2b537045f2d5eaa1229f619db314c3ba6a14d38b35dc0aa123 ;; \
      arm64) sha=4ae22576a78223d8ccdf5f3b3660a5cbb5288416bf0ca96afc4a36d195932b94 ;; \
      *) echo "unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    apt-get update && apt-get install -y --no-install-recommends curl ca-certificates; \
    curl -fsSL --proto '=https' --proto-redir '=https' --tlsv1.2 \
      -o /usr/local/bin/supercronic \
      "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-${TARGETARCH:-amd64}"; \
    echo "${sha}  /usr/local/bin/supercronic" | sha256sum -c -; \
    chmod +x /usr/local/bin/supercronic; \
    apt-get purge -y --auto-remove curl; \
    rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # Never prompt for an email or phone home from a container.
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

WORKDIR /app

# System deps: psycopg2-binary ships its libpq, but tzdata is needed for the
# timezone math the engines and the snapshot guard rely on (PORTFOLIODB_TZ).
#
# `apt-get upgrade` takes every Debian security fix published since the pinned
# base digest was built. It replaced a hand-maintained line naming zlib1g, which
# had to be noticed and removed once a base caught up, and only ever covered the
# one package the last scan happened to flag. The cost is that two builds of one
# commit on different days can carry different Debian patch releases. The
# Python set, which is what the tests exercise, stays exactly locked below; and
# publish.yml builds without a layer cache and scans before it pushes, so what
# ships is always the fresh build that was scanned. The weekly rebuild in
# publish.yml is what moves `:1` and `latest` onto new fixes between releases.
RUN apt-get update \
 && apt-get -y upgrade \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*

# Install Python deps first for better layer caching. requirements.txt is the
# ranges the code needs; constraints.txt is what they resolved to on the day the
# image was tested, generated inside this image (`make lock`). Installing with
# both means two builds of one commit contain the same packages, and CI checks
# that the built image's `pip freeze` still equals the file.
COPY app/requirements.txt /app/app/requirements.txt
COPY app/constraints.txt /app/app/constraints.txt
COPY app/requirements.lock /app/app/requirements.lock
# `--only-binary :all:` — wheels only, so no package's setup.py runs at build
# time; every pin in the lock has one.
#
# `--require-hashes` against app/requirements.lock: the same pins as
# constraints.txt, plus the sha256 of each wheel (tools/hash_lock.py). pip
# refuses any file whose bytes differ, so a tampered upload or a compromised
# mirror fails the build instead of shipping. The lock is the complete freeze,
# so the resolver still checks that the set is consistent, and `pip check`
# confirms it afterwards.
#
# pip itself is upgraded to the locked version first, from its own hashed line.
# It vendors its own certifi, msgpack and urllib3, and the base image's pip
# lags: 26.2.x bundles a certifi that still trusts the compromised emSign roots.
#
# PIP_LOCK=refresh is for `make lock` alone: it resolves requirements.txt under
# the old constraints, without hashes, so a requirement added since the last
# lock gets installed and frozen. Nothing published is built that way —
# publish.yml and CI build the default, and CI fails if the image's freeze and
# constraints.txt disagree.
ARG PIP_LOCK=hashes
RUN set -eu; \
    if [ "$PIP_LOCK" = refresh ]; then \
      pip install --only-binary :all: -c /app/app/constraints.txt --upgrade pip; \
      pip install --only-binary :all: -c /app/app/constraints.txt -r /app/app/requirements.txt; \
    else \
      grep -E '^pip==' /app/app/requirements.lock > /tmp/pip.lock; \
      pip install --require-hashes --only-binary :all: --no-deps -r /tmp/pip.lock; \
      rm /tmp/pip.lock; \
      pip install --require-hashes --only-binary :all: -r /app/app/requirements.lock; \
    fi; \
    pip check

# Bring the rest of the app/ tree (engines, dashboard, services, MCP server),
# the scheduler's crontab, and the SQL — the last one so an install that pulled
# this image without cloning the repository can still run apply_schema.py.
COPY app /app/app
COPY docker/crontab docker/crontab-llm /app/docker/
COPY sql /app/sql
# The lock's hash generator, so `make lock` can rehash inside the image (it is
# stdlib-only and runs nowhere else in the container).
COPY tools/hash_lock.py /app/tools/hash_lock.py
# server.json carries the release number the dashboard footer reads
# (app/version.py). It is also the MCP registry manifest.
COPY server.json /app/server.json

# The licence travels with the binary. An image is a distribution of the program,
# and the AGPL wants its terms to reach whoever received it — someone who pulled
# this image and never saw the repository had the `licenses` label and nothing to
# read. The source it corresponds to is the `image.source` label above.
COPY LICENSE /app/LICENSE

# /app only, so `app.mcp.*` resolves. Do NOT add /app/app here: PYTHONPATH is
# prepended ahead of site-packages, and /app/app contains mcp/ — which would
# shadow the installed `mcp` SDK that fastmcp imports and break the MCP server
# with the misleading "FastMCP server support is not installed".
#
# The bare sibling modules (db, settings, fifo, …) get resolved at runtime
# instead: app/mcp/__init__.py *appends* app/ (after site-packages, so the SDK
# still wins) for the MCP side, and the dashboard runs with app/ as its working
# directory the same way it does on a host.
ENV PYTHONPATH=/app

# Stamp the build so provenance works for image users. app_version() reads the
# .git directory otherwise, which the image deliberately does not carry — so
# without this the dashboard and every MCP response reported "build unknown".
ARG PORTFOLIODB_VERSION=unknown
ENV PORTFOLIODB_APP_VERSION=${PORTFOLIODB_VERSION}

# Ownership proof for the MCP Server Registry: it only accepts a server.json
# whose `name` matches this label on the image the entry points at. Must stay in
# lockstep with server.json at the repo root.
LABEL io.modelcontextprotocol.server.name="io.github.amosgeva/portfoliodb"

# Created here, before the chown below, so a named volume mounted at this path
# inherits appuser ownership from the image. Without it Docker initialises the
# volume root-owned and the (non-root) app cannot write its cache.
RUN mkdir -p /app/cache

# No EXPOSE. One image serves several commands, so listing both ports made
# *every* container advertise 8765/tcp in `docker ps` — including the dashboard,
# which only ever listens on 8501. That reads as "the dashboard is also serving
# MCP", which it is not. EXPOSE is documentation only; compose declares the
# ports it actually publishes.

# Run as an unprivileged user (defense in depth for the network-facing
# services): if the app process is compromised it is not root in the container.
RUN useradd --create-home --uid 10001 appuser \
 && chown -R appuser /app

# The dashboard is the default surface; compose sets `command:` per service, so
# nothing in the stack depends on this default. Run from /app/app so the bare
# imports resolve exactly as they do on a host ("run it from app/").
CMD ["sh", "-c", "cd /app/app && streamlit run streamlit_app.py --server.address 0.0.0.0 --server.port ${PORTFOLIODB_DASHBOARD_PORT:-8501}"]

# ── dev: the build/test tooling target ───────────────────────────────────────
# base plus the unprivileged user, pip included. `make lock` freezes this stage
# (`pip freeze --all`, so pip's own pin stays in app/constraints.txt and the
# hashed lock), `make test` installs pytest into it with `pip install --user`,
# and CI compares its freeze with the lock. Nothing publishes or runs it as a
# service; docker-compose.dev.yml builds it only as the `devtools` service.
FROM base AS dev
USER appuser

# ── runtime: what ships ──────────────────────────────────────────────────────
# The same filesystem minus the installer. pip is uninstalled by its own
# uninstaller (it removes the pip, pip3 and pip3.X entry points with the
# package), and ensurepip's bundled wheel goes too, so `python -m ensurepip`
# cannot put it back. The bytes of the base image's original pip remain in that
# lower layer as deleted files — nothing a container can import, run or have
# scanned (Trivy and an SBOM read the final filesystem), and no lower layer can
# be edited without forking the base image. The final check fails the build if
# any route to pip survived.
#
# Everything the app imports was installed in `base` and stays: CI runs both
# suites inside this stage, with pytest mounted read-only from outside, to prove
# nothing at runtime needs pip (or the pkg_resources it vendors).
FROM base AS runtime
RUN set -eu; \
    PIP_ROOT_USER_ACTION=ignore python -m pip uninstall --yes pip; \
    rm -f /usr/local/lib/python3.*/ensurepip/_bundled/*.whl; \
    if python -c 'import pip' 2>/dev/null; then echo "pip is still importable" >&2; exit 1; fi; \
    if ls /usr/local/bin/pip* 2>/dev/null; then echo "a pip entry point survived" >&2; exit 1; fi; \
    if python -m ensurepip --root /tmp/ensurepip-probe >/dev/null 2>&1; then echo "ensurepip can still restore pip" >&2; exit 1; fi; \
    rm -rf /tmp/ensurepip-probe
USER appuser
