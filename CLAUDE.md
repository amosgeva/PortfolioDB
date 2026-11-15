# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Environment & credentials

- Postgres runs in Docker (`docker-compose.yml`): host `127.0.0.1`, port `54320`, db `portfoliodb`, user `portfoliouser`.
- All Python entry points require `PORTFOLIODB_PASSWORD` in the environment. `app/db.py::load_config` raises if it is missing. Other `PORTFOLIODB_*` env vars override the docker-compose defaults. **Exception:** a process that brings its own role reads only where the database is, through `db.load_target()` (host, port, dbname; no credentials) — the MCP server does this on its read-only role, which is why the `mcp` compose service is not given the write password.
- `.env` at the repo root holds real secrets and is read by compose and by `app/db.py` alike. Runtime *settings* (display name, timezone, collector window, LLM provider) live in the `settings` table and are edited on the dashboard's Manage → Settings page; `.env` only bootstraps them and holds the secrets.
- The MCP server runs read-only: its pool forces `default_transaction_read_only=on`, and it connects as the SELECT-only `portfoliodb_ro` role named by `PORTFOLIODB_MCP_RO_USER`/`_PASSWORD` in `.env`, refusing to start without it. `make ro-role` (`app/create_ro_role.py`) creates the role with an explicit list of grants; re-run it after an upgrade that changes what the MCP reads, since it revokes and re-grants. **Exception:** `PORTFOLIODB_MCP_ALLOW_RW_FALLBACK=1` runs it on the application's credentials, with a warning on every start. A bearer token under 32 characters refuses to start, and a client on another machine needs its host name in `PORTFOLIODB_MCP_ALLOWED_HOSTS` (otherwise 421).
- The dashboard's write pages — Manage (including its Settings tab) and Advisor — need `PORTFOLIODB_DASHBOARD_PASSWORD`; unset means read-only, not open. The read-only views and Data Health need no password. Each write path calls `native_auth.require_unlocked()` immediately before it writes, so a disabled button is a courtesy and that call is the control. **Exception:** the CLIs and the scheduler run as the operator and do not use the password.
- Every published port binds to `127.0.0.1` (dashboard 8501, Postgres 54320, MCP 8765, pgAdmin 58080). **Exception:** LAN access is an explicit opt-in, a `ports: !override` block in `docker-compose.override.yml` — `docs/exposure.md` has it, and the tunnels that are safer.
- The database lives in the `pgdata` **named volume** by default. A host directory (`./data`) is opt-in through `docker-compose.override.yml` — which is what the operator's own box does. Wherever it lives, Postgres owns it: never edit, move, or delete files inside it.

## Common commands

Everything routes through the `Makefile`, which wraps `docker compose`. `make`
on its own lists every target.

The Makefile assumes a **POSIX** shell (the Unix command-line environment macOS
and Linux provide and Windows does not) — its recipes use `sed`, `base64`, `gzip`
and `/dev/urandom`, so it is not the Windows path. There are three descriptions
of how to run this project and they are meant to agree:

- `Makefile` — the runner on macOS and Linux.
- `docs/commands.md` — the canonical `make` → `docker compose` mapping.
- `pdb.ps1` — the Windows runner, implementing only the three targets with real
  logic (`init`, `backup`, `restore`).

So a change to one of those three targets, or to a compose invocation in the
Makefile, normally has to land in all three places, and CI enforces it
(`.github/workflows/ci.yml`, the `wrapper-*` jobs). **Exception:** a target that
is deliberately POSIX-only needs no `pdb.ps1` counterpart — add it to
`docs/commands.md` and to the `NOT_TRANSLATABLE` or `WRAPPED` set in
`tools/tests/test_runner_parity.py`, which is where that judgement is recorded.

**Do not hand-translate `backup`/`restore` for Windows.** There is no host
`gzip`, and PowerShell 5.1 decodes native-command output as text before
redirection, so piping a dump through `>` corrupts it silently and only fails at
restore time. Compress inside the container and move the file with
`docker compose cp` — see `docs/commands.md#backup`. **Exception:** on
PowerShell 7 the redirect is safe, so a one-off `docker compose exec` pipeline is
fine when you know the host is 7 and you verify the result with `gzip -t`.
Anything written down for someone else to run should still use the portable
form, because a reader on 5.1 gets a corrupt archive and no error.

```bash
make up          # postgres + dashboard (:8501) + scheduler
make schema      # create/refresh tables, then apply sql/migrations/* in order
make demo-seed   # fictional portfolio, for a fresh install or a screenshot
make ps          # service status
make logs ARGS=scheduler
make psql        # interactive psql
make test        # builds the image from the working tree, runs both fast suites in it
```

`make test` runs with a blank database password, so it never touches the
stack's database; the database-backed tests run in CI's `Test suites (live
database)` job (CONTRIBUTING.md shows the throwaway-Postgres version).

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

A repo-local venv at `.venv/` (gitignored) is the preferred home for that
install, so the project's pins are not resolved against whatever else shares a
system-wide interpreter. The launchers and `.githooks/pre-commit` look for
`.venv/Scripts/python.exe` (Windows) or `.venv/bin/python` (POSIX) first and
fall back to PATH, so a clone without a venv still runs — but a global install
means an unrelated project's upgrade can change what this one imports.

Enable the pre-commit hook (once per clone — hooks are not cloned):
```powershell
git config core.hooksPath .githooks
```
It runs the unused-import check on staged Python files, ruff on them when ruff
is installed (`tools/ruff.toml`, the rule set CI's `Lint (ruff)` job runs:
`ruff check --config tools/ruff.toml .`), and both test suites.
`git commit --no-verify` bypasses it for work in progress.

`main` is branch-protected: a pull request must be up to date with `main` and
pass the required checks — both test-suite jobs, `Lint (ruff)`, the image build
and both Windows runner jobs. Merge `main` in when it moves rather than
force-pushing. Every push to `main` publishes the image as `latest`.
**Exception:** `[skip publish]` in the merge commit's message skips publishing
that push, for a release assembled from several pull requests; a version tag
always publishes.

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

PortfolioDB is an append-only, lot-based personal portfolio ledger on Postgres, with Python CLIs for entry and reporting, a Streamlit dashboard, and a read-only MCP server.

### Data model (`sql/schema.sql`, `sql/schema_fd.sql`)
The ledger:
- `instruments` — symbol registry (`watchlist` flag marks non-held symbols to still snapshot; `currency`). The database refuses a symbol that does not match `instruments_symbol_format`; writers validate first with `symbol_paths.normalize_symbol`.
- `lots` — every BUY/SELL trade. `side` + positive `quantity` encode direction; a unique index on `(symbol, COALESCE(account,''), side, trade_date, quantity, price)` is a best-effort dedupe guard, and every writer reports a refused duplicate ("DUPLICATE — NOT RECORDED", exit 1 from the CLIs) instead of claiming success.
- `price_snapshots` — time-series quotes, PK `(symbol, ts)`, written by `snapshot_prices.py`; `last_price > 0` is enforced.
- `cash_snapshots` — manual cash balances (no auto-pull from brokers). Latest row per `account` wins.
- `income` — dividends, interest, distributions; a separate additive term, never part of cost basis.
- `corporate_actions` — splits, applied at read time (see the prepared ledger below).

Around it: `settings` (runtime settings, read DB → env → default through `app/settings.py`), `price_alerts` (Manage → Alerts), `lot_deletions`, `snapshot_runs` (the collector's run log, which price freshness is judged against), `advisor_briefs` / `chat_log`, `schema_migrations`, and the optional Financial Datasets `fd_*` tables in `sql/schema_fd.sql`.

**Rule:** the ledger is append-only — a correction is a new row, and nothing rewrites `lots` or `price_snapshots`. **Exception:** a lot deleted on the Manage page leaves `lots`, and its full row, the time and the operator's reason go to `lot_deletions` in the same statement; `settings`, instrument flags, `price_alerts.active` (alerts are deactivated, not deleted) and a split's `reviewed` flag are updated in place.

**Schema changes:** add a numbered, idempotent file under `sql/migrations/` and make the same change in `sql/schema.sql` (fresh installs read only that), bumping its version header and `SCHEMA_VERSION` in `app/mcp/services/cutoff.py`. `app/apply_schema.py` (`make schema`) applies `schema.sql` and `schema_fd.sql` every time and each migration once, recording filename and checksum in `schema_migrations`; it reports a migration whose text changed after it was applied, so a shipped migration stays as shipped. **Exception:** functions, views and triggers go in `schema.sql` as `CREATE OR REPLACE`, since it is re-applied on every upgrade. `sql/migrations/README.md` has the pattern for a CHECK on a table with existing rows.

**Rule:** there is **no positions table**. Open quantity, cost basis, and realized P&L are derived from `lots` on read and never stored, so a new lot changes every derived figure without a backfill step.

**Exception:** a read may be served from a short-lived cache rather than recomputed. The MCP positions service (`app/mcp/services/positions.py`) memoises its positions frame for 60s (`_FRAME_CACHE_TTL_SECONDS`; bypassed inside a review's single snapshot) and the dashboard caches its payload for 120s (`@st.cache_data(ttl=120)`), so a figure can be stale by up to that long. A cache entry is a memo of the computation, not a recorded position — the derivation is still the only source of truth. Keep both TTLs short and do not remove them: they exist because a backfill mutates history behind the cutoff, and a longer window would serve figures computed from lots that have since changed. If you need a guaranteed-fresh read, clear the cache (`clear_frame_cache()` / `build_payload.clear()`) rather than lengthening or deleting the TTL.

### P&L engines (`app/fifo.py`, `app/avg_cost.py`)
Two parallel engines over one `Lot` class (`ledger_core.Lot`), with `run_*` entry points:
- `fifo.run_fifo` — FIFO matching per `(symbol, account)`; returns per-match lines + open-buy queue.
- `avg_cost.run_avg_cost` — moving weighted average; simpler totals only.

Shared invariants across both:
- Matching is scoped to `(symbol, account)`; cross-account fungibility is intentional only at the merge step.
- BUY fees inflate cost basis, SELL fees reduce proceeds.
- Shorts are not supported — SELL exceeding open BUYs logs a warning and is truncated.
- Processing order is `ledger_core.processing_key`: trade date, then every BUY before any SELL that day, then id. A same-day sale stored before its purchase is still matched. Sort lots with that key, not by `id` or by insertion order.

**Rule:** "what is held" has one implementation — `holdings.net_quantities` (and `open_symbols` / `open_quantities` over it) in Python, `lot_open_quantities()` in SQL — both following the engines' rules (per account, processing order, an oversell truncated). `app/tests/test_ledger_sql.py` holds the two equal. **Exception:** a writer checking a proposed sale uses `oversell.check_sell()`, which runs the same walk with the new lot added.

### Prepared ledger (`app/ledger_inputs.py`) and aggregation (`app/portfolio.py`)
Readers get their lots from `ledger_inputs.load(conn)`. It reads them in FIFO processing order together with the recorded `corporate_actions` and restates them into post-split units. The `PreparedLedger` it returns also carries `price_by_day` / `price_points` / `price_rows`, which restate that reader's price series with the same actions. **This is the parity contract.** The dashboard payload, the positions CLI, both reports, the income backfill and the MCP positions service all go through it. A reader that queries `lots` or adjusts prices on its own disagrees with the others the day a split is recorded. That is what happened before the loader existed: a shared engine over differently prepared inputs is not parity. Apply each factor once. Never adjust lots and leave prices raw, or the reverse.

**Exception:** a reader that needs its own filter composes on `ledger_inputs.LOTS_SQL` and passes the rows to `ledger_inputs.prepare()`, as the MCP positions service does. The Manage page's trade-history table and the CSV importer read and write raw rows on purpose: they show and record what was entered. The source scan in `app/tests/test_ledger_inputs.py` keeps the list of such readers, with the reason next to each.

`compute_fifo_merged` / `compute_avg_cost_merged` then group the prepared rows by `(symbol, account)`, run the respective engine, and merge per symbol into a `DataFrame` with `symbol, qty, open_cost, avg_cost, realized_pnl`. Keep that output contract stable.

**Exception:** `corporate_actions.fetch_actions` returns an empty list only when the table does not exist (a database that never had the migration). Any other failure propagates: a transient error that read as "no actions" silently restated every figure into pre-split units, and a wrong cost basis is worse than an error.

**Rule:** only the actions in effect adjust anything. `fetch_actions` returns `corporate_actions.effective_actions()` of the recorded rows; the SQL statement of the same rule is the `corporate_actions_in_effect` view. A split the collector recorded is pending (`reviewed = FALSE`) and changes no figure — neither prices nor lots — until approved with `python app/check_splits.py --approve SYMBOL:YYYY-MM-DD --shares-credited` (or `--quantities-already-post-split`); `apply_schema.py` lists pending ones. A hand-entered row applies as entered, and two records of one split within seven days apply once (a trigger refuses the second). Confirm a suspected split against the vendor (`yfinance` `Ticker.splits`, which `check_splits.py` does) before recording one: a price ratio alone cannot tell a split from a crash. **Exception:** `fetch_recorded_actions` returns the whole register, pending rows included, for the scanners and reports that show it; it is not input for an adjuster.

**Rule:** the ledger is USD-only — no FX table, no conversion. Writers refuse what they can detect: `add_lot.py`, `sell_lot.py` and the CSV importer refuse a non-US listing suffix (`.TA`, `.L`, `.HK` …) or an instrument registered in another currency, and `add_income.py` refuses non-USD income. Readers leave non-USD income and cash out of every total and list them, and flag a non-USD instrument while keeping it in the valuation (`portfolio_kpis.currency_warnings()`, `docs/methodology.md` §12). **Exception:** `--allow-non-usd` records one anyway, and Manage asks for confirmation instead of refusing.

### Dashboard (`app/streamlit_app.py`)
Two halves in one Streamlit session. The read-only views (Portfolio through History, Alerts & News) are a custom front end rendered in a `components.html` iframe from `app/dashboard/static/` (`shell.html`, `app.css`, `app.js`), fed the JSON that `dashboard/payload.py` builds (SQL in `dashboard/queries.py`); it cannot write. Manage (tabs: Trades, Watchlist, Cash, Alerts, Settings, Export), Advisor and Data Health are native Streamlit pages in `modern2_native.py`, themed from the front end's own stylesheet, writing through `native_writes.py` behind `native_auth.py`. Moving between the halves is a rerun in the same session, not a page load, so an unlock and form drafts survive. The figures the payload, the reports, the Advisor and the MCP share come from `app/portfolio_kpis.py`. The front-end change in PR #122 turns `app.js` / `app.css` into an esbuild bundle of the ES modules under `static/src/` (`npm run build`, checked in CI); once it is merged, edit the modules and rebuild rather than editing the bundle.

### Layers
- `app/db.py` — psycopg2 connection + `fetch_all` / `execute` helpers. Everything in `app/` goes through this; the ad-hoc scripts at the repo root often open their own psycopg2 connections.
- `app/` — long-lived CLIs and modules (engines, dashboard, reports).
- Repo root — `Makefile` (the entry point for every command), `docker-compose.yml`, `docker/crontab`. Gitignored and host-specific: `run_*.ps1` / `setup_*.ps1` launchers and throwaway scripts under `archive/`. Prefer adding durable logic under `app/`.
- `app/tests/` — pytest suite for everything importable from `app/` (engines, loader, reports, importer, dashboard payload); `app/mcp/tests/` for the MCP server, run from the repo root.

### Snapshot collection
`snapshot_prices.py` selects symbols with open quantity (`ledger_inputs.open_symbols`, the one net quantity) OR `watchlist=TRUE`, pulls last/bid/ask via `yfinance` (`Ticker.info` only — `fast_info` is deliberately unused: its keys are camelCase, so the old `fi.get("last_price")` always returned `None` and every symbol fell through to `info` anyway, and it exposes neither bid/ask nor the trade timestamp the staleness guard needs), and inserts with `ON CONFLICT DO NOTHING`. It refuses a zero, negative or NaN price and stores Yahoo's out-of-hours `0.0` bid/ask as NULL; vendor-reported splits go to `corporate_actions` pending review. A run holds an advisory lock (a manual run and the scheduled one cannot overlap) and a whole-run deadline (`PORTFOLIODB_COLLECTOR_DEADLINE_S`). It refuses to collect outside the configured collector window (`app/market_window.py`, settable from the Settings page) unless `--ignore-window` is passed, so every caller obeys one rule. The `scheduler` service (supercronic, `docker/crontab`) is what invokes it on a schedule — see `docs/scheduling.md`.

## Conventions

- Monetary math uses `Decimal` end-to-end inside the engines; conversion to `float` happens only when writing to a DataFrame for display.
- Symbols are stored and compared uppercase; CLIs uppercase user input before querying.
- Throwaway/incident scripts live under `archive/`; none hardcode the DB password (verified 2026-07-18). Keep it that way — always use `db.load_config` for credentials in new code, or `db.load_target()` plus the process's own role when it must not hold the application's login (as the MCP pool does).
