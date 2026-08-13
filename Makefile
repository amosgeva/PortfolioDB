# PortfolioDB — one entry point for the containerized stack.
#
#   make            list every target
#   make up         start postgres + dashboard + scheduler
#   make schema     create/refresh the tables
#   make positions  show current positions
#   make add-lot ARGS="--symbol NVDA --account IBKR --trade-date 2026-02-13 --side BUY --qty 1 --price 184"
#
# Targets that take arguments read them from ARGS, so anything the underlying
# CLI accepts works without teaching this file about it.

COMPOSE ?= docker compose
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
        report demo-seed mcp tools backup restore ro-role

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
	write_key() { sed -i.bak -e "s|^$$1=.*|$$1=$$2|" .env; rm -f .env.bak; }; \
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

backup: ## pg_dump the database, gzipped — make backup ARGS=/other/dir
	@dir="$(if $(ARGS),$(ARGS),$(BACKUP_DIR))"; \
	mkdir -p "$$dir"; \
	out="$$dir/portfoliodb-$$(date +%Y%m%d-%H%M%S).sql.gz"; \
	$(COMPOSE) exec -T postgres pg_dump -U portfoliouser -d portfoliodb | gzip > "$$out"; \
	test -s "$$out" || { echo "backup is empty — is postgres running?"; rm -f "$$out"; exit 1; }; \
	echo "wrote $$out ($$(du -h "$$out" | cut -f1))"; \
	echo "Copy it off this machine, and keep .env + philosophy.md with it."

restore: ## Restore a dump into an EMPTY database — make restore ARGS=backups/x.sql.gz
	@test -n "$(ARGS)" || { echo "usage: make restore ARGS=backups/portfoliodb-....sql.gz"; exit 1; }
	@test -f "$(ARGS)" || { echo "no such file: $(ARGS)"; exit 1; }
	@n=$$($(PSQL) -tAc "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'" | tr -d '[:space:]'); \
	if [ "$$n" != "0" ]; then \
	  echo "Refusing to restore: the database already has $$n table(s)."; \
	  echo "Restoring over a live ledger is how data gets lost twice."; \
	  echo "To rebuild from scratch: make down && docker volume rm portfoliodb_pgdata && make up"; \
	  exit 1; \
	fi
	gunzip -c "$(ARGS)" | $(PSQL)
	@echo "restored $(ARGS)"
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

ask: ## Ask the advisor — make ask ARGS="what's my concentration risk?"
	$(RUN) python app/advisor.py ask $(ARGS)

report: ## Write the end-of-day text report
	$(RUN) python app/report_portfolio_db.py $(ARGS)

# ── development ──────────────────────────────────────────────────────────

shell: ## Shell inside a throwaway app container
	$(COMPOSE) run --rm --entrypoint sh dashboard

test: ## Run both test suites inside the container
	$(COMPOSE) run --rm dashboard sh -c '\
	  pip install --user --quiet pytest && \
	  export PATH=$$PATH:/home/appuser/.local/bin && \
	  echo "── app suite ──" && cd /app/app && python -m pytest tests/ -q && \
	  echo "── MCP suite ──" && cd /app && python -m pytest app/mcp/tests/ -m "not slow" -q'
