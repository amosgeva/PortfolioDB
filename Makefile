# PortfolioDB — one entry point for the containerized stack.
#
#   make            list every target
#   make up         start postgres + dashboard + scheduler
#   make schema     create/refresh the tables
#   make positions  show current positions
#   make add-lot ARGS="--symbol NVDA --account IBKR --trade-date 2026-02-13 --side BUY --qty 1 --price 184"
#
# Targets that take arguments read them from ARGS, so anything the underlying
# CLI accepts works without teaching this file about it. For the flag-taking
# targets ARGS is shell words on purpose — `--note 'manual update'` relies on
# the shell to keep the quoted note together. The two that take one free-form
# value (`ask`'s question, the backup/restore path) quote it whole instead, so an
# apostrophe in "what's my concentration risk?" is text, not shell syntax.

COMPOSE ?= docker compose
# Single-quote one value for the shell: every ' becomes '\'' . The result is
# safe anywhere a word is expected, but NOT inside double quotes.
shquote = '$(subst ','\'',$(1))'
# Where `make init` tells you to fetch a missing .env.template from.
RAW     := https://raw.githubusercontent.com/amosgeva/PortfolioDB/main
# Building from source is the contributor path, so it needs the dev overlay.
COMPOSE_DEV := $(COMPOSE) -f docker-compose.yml -f docker-compose.dev.yml
# One-shot container for CLIs: same image as the dashboard, removed after use.
RUN     := $(COMPOSE) run --rm dashboard
PSQL    := $(COMPOSE) exec -T postgres psql -q -U portfoliouser -d portfoliodb

BACKUP_DIR ?= backups

.DEFAULT_GOAL := help
.PHONY: help init up down restart build pull dev-up ps logs schema psql shell test \
        positions add-lot sell-lot set-cash watchlist snapshot brief ask \
        report demo-seed mcp tools backup restore ro-role lock

help: ## Show this list
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) \
	 | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[1m%-12s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "  Pass CLI flags with ARGS=\"...\" — e.g. make positions ARGS=\"--symbol NVDA\""

# ── first run ────────────────────────────────────────────────────────────

init: ## First run: fill .env with generated secrets (safe to re-run)
	@if [ ! -e .env ]; then \
	  if [ -e .env.template ]; then \
	    cp .env.template .env; \
	    echo "Created .env from .env.template."; \
	  else \
	    echo "No .env here, and no .env.template to copy from."; \
	    echo "Fetch one:"; \
	    echo "  curl -fsSL $(RAW)/.env.template -o .env"; \
	    exit 1; \
	  fi; \
	fi
	@set -e; \
	read_key() { sed -n "s/^$$1=//p" .env | head -1; }; \
	gen() { head -c 48 /dev/urandom | base64 | tr -d '/+=' | cut -c1-"$$1"; }; \
	write_key() { \
	  if grep -q "^$$1=" .env; then sed -i.bak -e "s|^$$1=.*|$$1=$$2|" .env; rm -f .env.bak; \
	  else [ -z "$$(tail -c1 .env)" ] || echo >> .env; printf '%s=%s\n' "$$1" "$$2" >> .env; fi; }; \
	pg=$$(read_key POSTGRES_PASSWORD); app=$$(read_key PORTFOLIODB_PASSWORD); \
	if [ -z "$$pg" ] && [ -z "$$app" ]; then \
	  pw=$$(gen 22); write_key POSTGRES_PASSWORD "$$pw"; write_key PORTFOLIODB_PASSWORD "$$pw"; \
	  echo "Generated a Postgres password and set both keys to it."; \
	elif [ -z "$$app" ]; then \
	  write_key PORTFOLIODB_PASSWORD "$$pg"; \
	  echo "PORTFOLIODB_PASSWORD was empty — set it to match POSTGRES_PASSWORD."; \
	elif [ -z "$$pg" ]; then \
	  write_key POSTGRES_PASSWORD "$$app"; \
	  echo "POSTGRES_PASSWORD was empty — set it to match PORTFOLIODB_PASSWORD."; \
	elif [ "$$pg" != "$$app" ]; then \
	  echo "WARNING: POSTGRES_PASSWORD and PORTFOLIODB_PASSWORD are different."; \
	  echo "         Postgres will start and the app will fail to connect."; \
	  echo "         Left both alone — make them equal by hand."; \
	else \
	  echo "Postgres password already set — left alone."; \
	fi; \
	if [ -z "$$(read_key PORTFOLIODB_MCP_TOKEN)" ]; then \
	  write_key PORTFOLIODB_MCP_TOKEN "$$(gen 40)"; \
	  echo "Generated an MCP token."; \
	else \
	  echo "MCP token already set — left alone."; \
	fi; \
	if [ -z "$$(read_key PORTFOLIODB_DASHBOARD_PASSWORD)" ]; then \
	  write_key PORTFOLIODB_DASHBOARD_PASSWORD "$$(gen 20)"; \
	  echo "Generated a dashboard password (the write pages ask for it; it is in .env)."; \
	else \
	  echo "Dashboard password already set — left alone."; \
	fi; \
	chmod 600 .env; \
	echo "Tightened permissions on .env to 600."
	@echo ""
	@echo "Next:"
	@echo "  1. make up && make schema"
	@echo "  2. make demo-seed   # fictional data to look at, skip for a real ledger"
	@echo "  3. open http://localhost:8501"
	@echo ""
	@echo "  Optional: an LLM API key in .env enables the advisor"
	@echo "            (docs/llm-providers.md), and your investor one-pager is"
	@echo "            pasted into the dashboard's Advisor tab (docs/philosophy.md)."

# ── stack ────────────────────────────────────────────────────────────────

up: ## Start postgres + dashboard + scheduler (detached)
	$(COMPOSE) up -d

down: ## Stop everything (keeps the database volume)
	$(COMPOSE) down

restart: ## Recreate the app containers, e.g. after changing .env
	$(COMPOSE) up -d --force-recreate dashboard scheduler

pull: ## Fetch the latest published application image
	$(COMPOSE) pull

build: ## Build the application image from source (contributors)
	PORTFOLIODB_VERSION=$$(git rev-parse --short HEAD 2>/dev/null || echo dev) $(COMPOSE_DEV) build

dev-up: ## Start the stack from a locally built image
	$(COMPOSE_DEV) up -d

# Regenerated INSIDE the image, never from a host interpreter: the lock has to
# be the resolution the image's Python and platform produce, or CI's
# "image matches the lock" check rejects it. Four steps:
#   1. build with PIP_LOCK=refresh, which resolves requirements.txt under the
#      current constraints *without* hashes, so a newly added requirement is
#      installed at all (the normal build installs only what the lock lists);
#   2. freeze that image into app/constraints.txt;
#   3. hash every wheel of every pin into app/requirements.lock
#      (tools/hash_lock.py, run in the same image; it reads the new pins on
#      stdin and asks PyPI for the files);
#   4. build normally, which installs from the new lock with --require-hashes —
#      so a lock that cannot install fails here, not in CI.
lock: ## Refresh app/constraints.txt and the hashed app/requirements.lock (after editing requirements.txt)
	$(COMPOSE_DEV) build --build-arg PIP_LOCK=refresh dashboard
	@{ sed -n '/^#/p' app/constraints.txt; \
	   $(COMPOSE_DEV) run --rm --no-deps -T dashboard pip freeze --all --exclude-editable; } > app/constraints.txt.new \
	&& mv app/constraints.txt.new app/constraints.txt \
	&& echo "app/constraints.txt refreshed: $$(grep -vc '^#' app/constraints.txt) pins"
	@$(COMPOSE_DEV) run --rm --no-deps -T dashboard python /app/tools/hash_lock.py - < app/constraints.txt > app/requirements.lock.new \
	&& mv app/requirements.lock.new app/requirements.lock \
	&& echo "app/requirements.lock rehashed"
	$(COMPOSE_DEV) build dashboard
	@echo "the image builds from the new lock — review both diffs and commit them together"

ps: ## Show service status
	$(COMPOSE) ps

logs: ## Follow logs — make logs ARGS=scheduler for one service
	$(COMPOSE) logs -f $(ARGS)

mcp: ## Start the optional MCP server (:8765)
	$(COMPOSE) --profile mcp up -d mcp

tools: ## Start the optional pgAdmin browser (:58080)
	$(COMPOSE) --profile tools up -d pgadmin

# ── database ─────────────────────────────────────────────────────────────

schema: ## Create/refresh tables, then apply migrations in order
	$(RUN) python app/apply_schema.py $(ARGS)

psql: ## Open an interactive psql shell
	$(COMPOSE) exec postgres psql -U portfoliouser -d portfoliodb

demo-seed: ## Load a fictional portfolio so a fresh install has something to show
	$(RUN) python app/demo_seed.py --yes $(ARGS)

ro-role: ## Create the read-only role for the MCP server (PASSWORD= optional)
	$(RUN) python app/create_ro_role.py $(if $(PASSWORD),--password $(PASSWORD),--generate)

# ── backup / restore ─────────────────────────────────────────────────────

# Three checks, because a pipeline's exit status is its LAST command's: a
# pg_dump that died still fed gzip a valid, non-empty archive of nothing, and
# `test -s` waved it through. So (1) the dump and gzip run under `bash -o
# pipefail` inside the container, which makes a producer failure the pipeline's
# failure; (2) the archive must decompress; (3) it must carry pg_dump's own
# completion marker, which a dump killed mid-way never writes. The file is built
# under a .part name and renamed only once all three pass, so a failed run never
# leaves something that looks like a backup.
backup: ## pg_dump the database, gzipped and verified — make backup ARGS=/other/dir
	@dir=$(call shquote,$(if $(ARGS),$(ARGS),$(BACKUP_DIR))); \
	mkdir -p "$$dir"; \
	out="$$dir/portfoliodb-$$(date +%Y%m%d-%H%M%S).sql.gz"; \
	if ! $(COMPOSE) exec -T postgres bash -o pipefail -c \
	     'pg_dump -U portfoliouser -d portfoliodb | gzip -c' > "$$out.part"; then \
	  rm -f "$$out.part"; \
	  echo "backup FAILED: pg_dump or gzip exited non-zero — nothing was written. Is postgres running?"; \
	  exit 1; \
	fi; \
	if ! gzip -t "$$out.part" 2>/dev/null; then \
	  rm -f "$$out.part"; echo "backup FAILED: the archive is not a valid gzip — nothing was kept"; exit 1; \
	fi; \
	if ! gunzip -c "$$out.part" | grep -q 'PostgreSQL database dump complete'; then \
	  rm -f "$$out.part"; \
	  echo "backup FAILED: the dump has no completion marker, so pg_dump did not finish — nothing was kept"; \
	  exit 1; \
	fi; \
	mv "$$out.part" "$$out"; \
	echo "wrote $$out ($$(du -h "$$out" | cut -f1))"; \
	echo "Copy it off this machine, and keep .env + philosophy.md with it."

restore: ## Restore a dump into an EMPTY database — make restore ARGS=backups/x.sql.gz
	@test -n $(call shquote,$(ARGS)) || { echo "usage: make restore ARGS=backups/portfoliodb-....sql.gz"; exit 1; }
	@test -f $(call shquote,$(ARGS)) || { echo "no such file: "$(call shquote,$(ARGS)); exit 1; }
	@n=$$($(PSQL) -tAc "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'" | tr -d '[:space:]'); \
	if [ "$$n" != "0" ]; then \
	  echo "Refusing to restore: the database already has $$n table(s)."; \
	  echo "Restoring over a live ledger is how data gets lost twice."; \
	  echo "To rebuild from scratch: make down && docker volume rm portfoliodb_pgdata && make up"; \
	  exit 1; \
	fi
	@gzip -t $(call shquote,$(ARGS)) 2>/dev/null || { \
	  echo "not a valid gzip: "$(call shquote,$(ARGS))" — the archive is truncated or corrupt, nothing was restored"; exit 1; }
	@# psql keeps going after a SQL error by default and exits 0, so a half-loaded
	@# database used to print "restored". ON_ERROR_STOP makes the first error fatal
	@# (exit 3) and --single-transaction rolls everything back, leaving the target
	@# as empty as the guard above found it. The archive was verified whole just
	@# above, which is why a mid-stream gunzip failure needs no separate check.
	@#
	@# A dump taken from a database whose public schema had been dropped and
	@# recreated carries `CREATE SCHEMA public;`, which a fresh database rejects —
	@# an error psql used to skip and now stops on. The guard proved the target
	@# holds no tables, so when the dump wants to create the schema, drop the empty
	@# one first, inside the same transaction so a failed restore puts it back.
	@pre=""; f=$(call shquote,$(ARGS)); \
	if gunzip -c "$$f" | grep -q '^CREATE SCHEMA public;'; then pre="DROP SCHEMA IF EXISTS public CASCADE;"; fi; \
	if ! { echo "$$pre"; gunzip -c "$$f"; } | $(PSQL) -X -v ON_ERROR_STOP=1 --single-transaction; then \
	  echo "restore FAILED: psql stopped at the first error and rolled back — the database is still empty"; \
	  exit 1; \
	fi
	@echo "restored "$(call shquote,$(ARGS))
	@$(PSQL) -tAc "SELECT 'lots: '||count(*) FROM lots"

# ── ledger ───────────────────────────────────────────────────────────────

positions: ## Current positions (FIFO)
	$(RUN) python app/positions.py $(ARGS)

add-lot: ## Record a BUY/SELL lot — needs ARGS
	$(RUN) python app/add_lot.py $(ARGS)

sell-lot: ## Record a sale against open lots — needs ARGS
	$(RUN) python app/sell_lot.py $(ARGS)

set-cash: ## Record a cash balance — needs ARGS
	$(RUN) python app/set_cash.py $(ARGS)

watchlist: ## Track symbols you don't hold — make watchlist ARGS="NVDA AMD"
	$(RUN) python app/set_watchlist.py $(ARGS)

# ── jobs (the scheduler runs these too) ───────────────────────────────────

snapshot: ## Collect prices now, ignoring the market window
	$(RUN) python app/snapshot_prices.py --ignore-window $(ARGS)

brief: ## Generate an advisor brief now
	$(RUN) python app/advisor.py brief $(ARGS)

# The question is one argument however it is worded: quoted whole, so an
# apostrophe or a `?` is text (audit E-7 — the example below was a syntax error).
ask: ## Ask the advisor — make ask ARGS="what's my concentration risk?"
	$(RUN) python app/advisor.py ask $(call shquote,$(ARGS))

# report_portfolio_db.py requires --mode; the target used to omit it and fail
# with a usage error. End-of-day unless ARGS says otherwise.
report: ## Write the end-of-day text report — ARGS="--mode daily" for the morning one
	$(RUN) python app/report_portfolio_db.py $(if $(ARGS),$(ARGS),--mode eod)

# ── development ──────────────────────────────────────────────────────────

shell: ## Shell inside a throwaway app container
	$(COMPOSE) run --rm --entrypoint sh dashboard

# Tests the working tree, not the published image (audit C-4): the dev overlay
# builds the image from this checkout first — cached, so only changed layers
# rebuild — and the suites run in that build. It never touches the stack's
# database: --no-deps leaves postgres alone and the blank password makes every
# database-backed test skip, so `make test` on a machine holding a real ledger
# writes nothing to it. The database-backed tests run in CI against a
# disposable Postgres (CONTRIBUTING.md shows how to do the same locally).
# `-p no:cacheprovider` because the container's root filesystem is read-only.
test: ## Build the image from the working tree and run both fast suites in it
	$(COMPOSE_DEV) build dashboard
	$(COMPOSE_DEV) run --rm --no-deps -e PORTFOLIODB_PASSWORD= dashboard sh -c '\
	  pip install --user --quiet pytest && \
	  export PATH=$$PATH:/home/appuser/.local/bin && \
	  echo "── app suite ──" && cd /app/app && python -m pytest -p no:cacheprovider tests/ -q && \
	  echo "── MCP suite ──" && cd /app && python -m pytest -p no:cacheprovider app/mcp/tests/ -m "not slow" -q'
