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

FROM python:3.13-slim

# supercronic: cron built for containers — logs every job to stdout, needs no
# root, and refuses to start on an invalid crontab instead of silently doing
# nothing (the failure mode that makes plain cron in Docker so unpleasant).
#
# Pinned by version and verified by SHA-256 per architecture, so a rebuild can
# never silently swap the binary. TARGETARCH is supplied by BuildKit, which
# keeps this working for the multi-arch build in Phase 6.
ARG SUPERCRONIC_VERSION=v0.2.33
ARG TARGETARCH
RUN set -eu; \
    case "${TARGETARCH:-amd64}" in \
      amd64) sha=feefa310da569c81b99e1027b86b27b51e6ee9ab647747b49099645120cfc671 ;; \
      arm64) sha=f1f8585c66de020fef494dd636058f99949d108f569fef00016a1c8b9eb145b3 ;; \
      *) echo "unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    apt-get update && apt-get install -y --no-install-recommends curl ca-certificates; \
    curl -fsSL -o /usr/local/bin/supercronic \
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
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*

# Install Python deps first for better layer caching.
COPY app/requirements.txt /app/app/requirements.txt
RUN pip install -r /app/app/requirements.txt

# Bring the rest of the app/ tree (engines, dashboard, services, MCP server),
# the scheduler's crontab, and the SQL — the last one so an install that pulled
# this image without cloning the repository can still run apply_schema.py.
COPY app /app/app
COPY docker/crontab /app/docker/crontab
COPY sql /app/sql

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
