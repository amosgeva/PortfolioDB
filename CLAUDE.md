# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Environment & credentials

- Postgres runs in Docker (`docker-compose.yml`): host `127.0.0.1`, port `54320`, db `portfoliodb`, user `portfoliouser`.
- All Python entry points require `PORTFOLIODB_PASSWORD` in the environment. `app/db.py::load_config` raises if it is missing. Other `PORTFOLIODB_*` env vars override the docker-compose defaults.
- `.env` at the repo root holds real secrets and is read by compose and by `app/db.py` alike. Runtime *settings* (display name, timezone, collector window, LLM provider) live in the `settings` table and are edited on the dashboard's Manage → Settings page; `.env` only bootstraps them and holds the secrets.
- The MCP server runs read-only: its pool forces `default_transaction_read_only=on`, and it uses the SELECT-only `portfoliodb_ro` role when `PORTFOLIODB_MCP_RO_USER`/`_PASSWORD` are set in `.env` (create the role with `sql/create_ro_role.sql`).
- The database lives in the `pgdata` **named volume** by default. A host directory (`./data`) is opt-in through `docker-compose.override.yml` — which is what the operator's own box does. Wherever it lives, Postgres owns it: never edit, move, or delete files inside it.

## Common commands

Everything routes through the `Makefile`, which wraps `docker compose`. `make`
on its own lists every target.

```bash
make up          # postgres + dashboard (:8501) + scheduler
make schema      # create/refresh tables, then apply sql/migrations/* in order
make demo-seed   # fictional portfolio, for a fresh install or a screenshot
make ps          # service status
make logs ARGS=scheduler
make psql        # interactive psql
make test        # both suites, inside the container
```

Ledger CLIs take their flags through `ARGS`:

```bash
make positions
make positions ARGS="--symbol NVDA"
make add-lot  ARGS="--symbol NVDA --account IBKR --trade-date 2026-02-13 --side BUY --qty 1 --price 184.00"
make sell-lot ARGS="--symbol NVDA --account IBKR --trade-date 2026-03-01 --qty 1 --price 200.00 --fees 2.50"
make set-cash ARGS="--cash 1000 --account IBKR --note 'manual update'"
make watchlist ARGS="NVDA AMD"
make snapshot    # collect prices now, ignoring the market window
make brief       # advisor brief now
```

Without `make`, each target is a one-liner you can run directly — e.g.
`docker compose run --rm dashboard python app/positions.py`.

Running Python on the host instead of in a container still works (the modules
have no container assumptions): install `app/requirements.txt`, set
`PORTFOLIODB_PASSWORD`, and run the scripts from `app/`. The operator's Windows
box does exactly that through gitignored `run_*.ps1` launchers, which is why
those are absent from a fresh clone.

Enable the pre-commit hook (once per clone — hooks are not cloned):
```powershell
git config core.hooksPath .githooks
```
It runs the unused-import check on staged Python files and both test suites.
`git commit --no-verify` bypasses it for work in progress.

Tests — the two suites have **different working directories**:
```powershell
# FIFO suite imports bare modules (`from fifo import ...`) — run from app/
cd app; pytest tests/
pytest tests/test_fifo.py::TestSingleBuy -v   # single class / test

# MCP suite imports `from app.mcp...` — run from the REPO ROOT, not app/.
# Running it from app/ puts the local `app/mcp/` package on the path as
# top-level `mcp`, shadowing the official `mcp` SDK that fastmcp needs
# (fails as a misleading "FastMCP server support is not installed").
pytest app/mcp/tests/

# The reconciliation suite runs the real services against the live database and
# takes ~30s, so it is marked `slow` and skipped by the pre-commit hook.
pytest app/mcp/tests/ -m slow          # just it
pytest app/mcp/tests/ -m "not slow"    # everything else (what the hook runs)
```

## Architecture

PortfolioDB is an append-only, lot-based personal portfolio ledger on Postgres, with Python scripts for entry/reporting and a Streamlit dashboard.

### Data model (`sql/schema.sql`)
Four tables, all append-only by design:
- `instruments` — symbol registry (`watchlist` flag marks non-held symbols to still snapshot).
- `lots` — every BUY/SELL trade. `side` + positive `quantity` encode direction; a unique index on `(symbol, account, trade_date, quantity, price)` is a best-effort dedupe guard for CSV imports.
- `price_snapshots` — time-series quotes, PK `(symbol, ts)`, written by `snapshot_prices.py`.
- `cash_snapshots` — manual cash balances (no auto-pull from brokers). Latest row per `account` wins.

There is **no positions table**: open quantity, cost basis, and realized P&L are derived from `lots` on read, never stored. Caching is the one qualifier — `positions.py` memoises its lot frame for 60s (`_FRAME_CACHE_TTL_SECONDS`) and the dashboard caches its payload for 120s — so a read can be that stale, but a cache entry is a memo of the computation, not a recorded position. Don't remove those TTLs: they exist because a backfill mutates history behind the cutoff.

### P&L engines (`app/fifo.py`, `app/avg_cost.py`)
Two parallel engines with matching `Lot` dataclasses and `run_*` entry points:
- `fifo.run_fifo` — FIFO matching per `(symbol, account)`; returns per-match lines + open-buy queue.
- `avg_cost.run_avg_cost` — moving weighted average; simpler totals only.

Shared invariants across both:
- Matching is scoped to `(symbol, account)`; cross-account fungibility is intentional only at the merge step.
- BUY fees inflate cost basis, SELL fees reduce proceeds.
- Shorts are not supported — SELL exceeding open BUYs logs a warning and is truncated.

### Portfolio aggregation (`app/portfolio.py`)
`compute_fifo_merged` / `compute_avg_cost_merged` group raw lot rows by `(symbol, account)`, run the respective engine, then merge per symbol into a `DataFrame` with `symbol, qty, open_cost, avg_cost, realized_pnl`. **This is the single entry point used by `streamlit_app.py`, `positions.py`, and `report_portfolio_db.py`** — keep its output contract stable.

### Layers
- `app/db.py` — psycopg2 connection + `fetch_all` / `execute` helpers. Everything in `app/` goes through this; the ad-hoc scripts at the repo root often open their own psycopg2 connections.
- `app/` — long-lived CLIs and modules (engines, dashboard, reports).
- Repo root — `Makefile` (the entry point for every command), `docker-compose.yml`, `docker/crontab`. Gitignored and host-specific: `run_*.ps1` / `setup_*.ps1` launchers and throwaway scripts under `archive/`. Prefer adding durable logic under `app/`.
- `app/tests/` — pytest suite, currently only FIFO coverage.

### Snapshot collection
`snapshot_prices.py` selects symbols with open quantity OR `watchlist=TRUE`, pulls last/bid/ask via `yfinance` (`fast_info` first, `info` fallback), and inserts with `ON CONFLICT DO NOTHING`. It refuses to collect outside the configured collector window (`app/market_window.py`, settable from the Settings page) unless `--ignore-window` is passed, so every caller obeys one rule. The `scheduler` service (supercronic, `docker/crontab`) is what invokes it on a schedule — see `docs/scheduling.md`.

## Conventions

- Monetary math uses `Decimal` end-to-end inside the engines; conversion to `float` happens only when writing to a DataFrame for display.
- Symbols are stored and compared uppercase; CLIs uppercase user input before querying.
- Throwaway/incident scripts live under `archive/`; none hardcode the DB password (verified 2026-07-18). Keep it that way — always use `db.load_config` for credentials in new code.
