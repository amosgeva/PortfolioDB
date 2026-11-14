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

# Pinned by digest, not by tag. `python:3.14-slim` is a moving target — a
# rebuild of the same commit a month later got a different Debian snapshot and
# a different patch release, so "the image CI tested" and "the image a user
# pulled" were only nominally the same. The digest below is the multi-arch
# index; Dependabot (.github/dependabot.yml) proposes the bump when the tag
# moves, and the weekly scan (security-scan.yml) says when a bump is overdue.
FROM python:3.14-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f

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
# zlib1g is named so apt upgrades it from trixie-security: the pinned base
# carries 1:1.3.dfsg+really1.3.1-1+b1, which has two HIGH advisories that
# 1+deb13u1 fixes. Drop it from this line once a base digest ships deb13u1.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata zlib1g \
 && rm -rf /var/lib/apt/lists/*

# Install Python deps first for better layer caching. requirements.txt is the
# ranges the code needs; constraints.txt is what they resolved to on the day the
# image was tested, generated inside this image (`make lock`). Installing with
# both means two builds of one commit contain the same packages, and CI checks
# that the built image's `pip freeze` still equals the file.
COPY app/requirements.txt /app/app/requirements.txt
COPY app/constraints.txt /app/app/constraints.txt
# `--only-binary :all:` — wheels only, so no package's setup.py runs at build
# time; every pin in the lock has one.
#
# pip itself is upgraded to the locked version first. It vendors its own
# certifi, msgpack and urllib3, and the base image's pip lags: 26.2.x bundles a
# certifi that still trusts the compromised emSign roots, which pip would use
# to verify its own downloads.
RUN pip install --only-binary :all: -c /app/app/constraints.txt --upgrade pip \
 && pip install --only-binary :all: -c /app/app/constraints.txt -r /app/app/requirements.txt

# Bring the rest of the app/ tree (engines, dashboard, services, MCP server),
# the scheduler's crontab, and the SQL — the last one so an install that pulled
# this image without cloning the repository can still run apply_schema.py.
COPY app /app/app
COPY docker/crontab /app/docker/crontab
COPY sql /app/sql
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
USER appuser

# The dashboard is the default surface; compose sets `command:` per service, so
# nothing in the stack depends on this default. Run from /app/app so the bare
# imports resolve exactly as they do on a host ("run it from app/").
CMD ["sh", "-c", "cd /app/app && streamlit run streamlit_app.py --server.address 0.0.0.0 --server.port ${PORTFOLIODB_DASHBOARD_PORT:-8501}"]
