# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Environment & credentials

- Postgres runs in Docker (`docker-compose.yml`): host `127.0.0.1`, port `54320`, db `portfoliodb`, user `portfoliouser`.
- All Python entry points require `PORTFOLIODB_PASSWORD` in the environment. `app/db.py::load_config` raises if it is missing. Other `PORTFOLIODB_*` env vars override the docker-compose defaults. **Exception:** a process that brings its own role reads only where the database is, through `db.load_target()` (host, port, dbname; no credentials) — the MCP server does this on its read-only role, which is why the `mcp` compose service is not given the write password.
- `.env` at the repo root holds real secrets and is read by compose and by `app/db.py` alike. Runtime *settings* (display name, timezone, collector window, LLM provider) live in the `settings` table and are edited on the dashboard's Manage → Settings page; `.env` only bootstraps them and holds the secrets.
- The MCP server runs read-only: its pool forces `default_transaction_read_only=on`.
- The MCP server connects as the SELECT-only `portfoliodb_ro` role from `PORTFOLIODB_MCP_RO_USER`/`_PASSWORD`. Without them the process still starts, but it opens no database connection: `/healthz` answers 503, every database-backed tool call fails, and the server log names the two missing lines. **Exception:** `PORTFOLIODB_MCP_ALLOW_RW_FALLBACK=1` connects on the application's credentials, with a warning on every start.
- `make ro-role` (`app/create_ro_role.py`) creates that role. Re-run it after an upgrade that changes what the MCP reads: it revokes and re-grants.
- By default the MCP server refuses to start with a bearer token under 32 characters or with fewer than 10 distinct ones (`app/mcp/auth.py`). **Exception:** `PORTFOLIODB_MCP_ALLOW_WEAK_TOKEN=1` starts it anyway with a logged warning (`docs/exposure.md`).
- A client on another machine needs its host name in `PORTFOLIODB_MCP_ALLOWED_HOSTS`; other hosts are refused (421).
- The dashboard's write pages need `PORTFOLIODB_DASHBOARD_PASSWORD`: Manage (with its Settings tab) and Advisor. Unset means read-only, not open. The read-only views and Data Health need no password.
- Each dashboard write calls `native_auth.require_unlocked()` just before it writes. That call is the control; a disabled button is a courtesy. **Exception:** the CLIs and the scheduler do not use the password.
- Every published port binds to `127.0.0.1`: dashboard 8501, Postgres 54320, MCP 8765, pgAdmin 58080. **Exception:** reaching the dashboard from the local network is an opt-in `ports: !override` block in `docker-compose.override.yml` (see `docs/exposure.md`).
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
make up          # postgres + dashboard (:8501) + scheduler + scheduler-llm
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

`main` is branch-protected. A pull request merges once it is up to date with `main` and passes the eight required checks: `Test suites (no database)`, `Test suites (live database)`, `Lint (ruff)`, `Application image (build and self-test)`, `Windows runner (init, both PowerShell hosts)`, `Windows runner (backup/restore round trip)`, and the front end's `Build check, lint, tests` and `Browser integration (Streamlit + Postgres)`. Publishing waits on the same CI and front-end workflows. When `main` moves, merge it in rather than force-pushing.

Every push to `main` publishes the image as `latest`. **Exception:** `[skip publish]` in the merge commit's message skips publishing that push; a version tag publishes regardless.

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

# The reconciliation suite runs the real services against a database seeded
# with the demo ledger and takes ~30s, so it is marked `slow` and skipped by the
# pre-commit hook and `make test`.
pytest app/mcp/tests/ -m slow          # just it
pytest app/mcp/tests/ -m "not slow"    # everything else (what the hook runs)
```

The two halves no longer interfere: against a seeded database the whole MCP suite passes in one process. CI still runs them as separate steps, the slow one first while the database holds exactly the demo ledger.

## Architecture

PortfolioDB is an append-only, lot-based personal portfolio ledger on Postgres, with Python CLIs for entry and reporting, a Streamlit dashboard, and a read-only MCP server.

### Data model (`sql/schema.sql`, `sql/schema_fd.sql`)
The ledger:
- `instruments` — symbol registry (`watchlist` flag marks non-held symbols to still snapshot; `currency`). The database refuses a symbol that does not match `instruments_symbol_format`; writers validate first with `symbol_paths.normalize_symbol`.
- `lots` — every trade, buy or sell. `side` + positive `quantity` encode direction. A unique index on `(symbol, account, side, trade_date, quantity, price)` is a best-effort dedupe guard (a null account counts as empty). Every writer reports a refused duplicate instead of claiming success.
- `price_snapshots` — time-series quotes, primary key `(symbol, ts)`, written by `snapshot_prices.py`. The database refuses a `last_price` of zero or below.
- `cash_snapshots` — manual cash balances (no auto-pull from brokers). Latest row per `account` wins.
- `income` — dividends, interest and distributions. Income adds to total return and stays out of cost basis.
- `corporate_actions` — splits, applied at read time (see the prepared ledger below).

Around the ledger:
- `settings` — runtime settings, read DB → env → default through `app/settings.py`.
- `price_alerts` — written from Manage → Alerts.
- `lot_deletions` — the audit row for each lot deleted on the Manage page.
- `snapshot_runs` — the collector's run log, which price freshness is judged against.
- `advisor_briefs` and `chat_log` — the Advisor's history.
- `schema_migrations` — which migrations `apply_schema.py` has applied.
- `fd_*` — the optional Financial Datasets tables, in `sql/schema_fd.sql`.

**Rule:** the ledger is append-only. A correction is a new row, and nothing rewrites `lots` or `price_snapshots`. **Exception:** deleting a lot on the Manage page removes it from `lots` and writes the full row, the time and the reason to `lot_deletions` in the same statement.

A few rows are updated in place: `settings`, instrument flags, `price_alerts.active` (alerts are deactivated, not deleted) and a split's `reviewed` flag.

**Schema changes:**
- Add a numbered, idempotent file under `sql/migrations/`, and make the same change in `sql/schema.sql`. `schema.sql` stays the complete current core schema, mirroring every migration.
- Bump the version header in `schema.sql` and `SCHEMA_VERSION` in `app/mcp/services/cutoff.py`.
- `app/apply_schema.py` (`make schema`) is the one installer, for fresh and existing databases alike. It applies `schema.sql`, then `schema_fd.sql`, then each migration not yet recorded.
- The installer records each migration's checksum in `schema_migrations` and reports one whose text changed after it was applied. So fix a shipped migration with a new one.
- Functions, views and triggers go in `schema.sql` as replaceable definitions, because that file is re-applied on every upgrade.
- `sql/migrations/README.md` has the pattern for adding a check constraint to a table with existing rows.

**Rule:** there is **no positions table**. Open quantity, cost basis, and realized P&L are derived from `lots` on read and never stored, so a new lot changes every derived figure without a backfill step.

**Exception:** a read may be served from a short-lived cache rather than recomputed. The MCP positions service (`app/mcp/services/positions.py`) memoises its positions frame for 60s (`_FRAME_CACHE_TTL_SECONDS`), except inside a review's single snapshot. The dashboard caches its payload for 120s (`@st.cache_data(ttl=120)`). A figure can be stale by up to that long.

A cache entry is a memo of the computation, not a recorded position; the derivation is still the source of truth. Keep both lifetimes short, and keep them. A backfill mutates history behind the cutoff, and a longer window would serve figures computed from lots that have since changed.

**Exception:** for a guaranteed-fresh read, clear the cache (`clear_frame_cache()` / `build_payload.clear()`) rather than lengthening or deleting its lifetime.

### P&L engines (`app/fifo.py`, `app/avg_cost.py`)
Two parallel engines over one `Lot` class (`ledger_core.Lot`), with `run_*` entry points:
- `fifo.run_fifo` — FIFO matching per `(symbol, account)`; returns per-match lines + open-buy queue.
- `avg_cost.run_avg_cost` — moving weighted average; simpler totals only.

Shared invariants across both:
- Matching is scoped to `(symbol, account)`; cross-account fungibility is intentional only at the merge step.
- BUY fees inflate cost basis, SELL fees reduce proceeds.
- Shorts are not supported — SELL exceeding open BUYs logs a warning and is truncated.
- Processing order is `ledger_core.processing_key`: trade date, then every BUY before any SELL that day, then id. A same-day sale stored before its purchase is still matched. Sort lots with that key, not by `id` or by insertion order.

**Rule:** "what is held" has one implementation. In Python it is `holdings.net_quantities`, with `open_symbols` / `open_quantities` over it; in SQL it is `lot_open_quantities()`. Both follow the engines' rules: per account, in processing order, with an oversell truncated. `app/tests/test_ledger_sql.py` holds the two equal. **Exception:** a writer checking a proposed sale uses `oversell.check_sell()`, which walks the stream with the new lot added.

### Prepared ledger (`app/ledger_inputs.py`) and aggregation (`app/portfolio.py`)
Readers get their lots from `ledger_inputs.load(conn)`. It reads them in FIFO processing order together with the recorded `corporate_actions` and restates them into post-split units. The `PreparedLedger` it returns also carries `price_by_day` / `price_points` / `price_rows`, which restate that reader's price series with the same actions. **This is the parity contract.** The dashboard payload, the positions CLI, both reports, the income backfill and the MCP positions service all go through it. A reader that queries `lots` or adjusts prices on its own disagrees with the others the day a split is recorded. That is what happened before the loader existed: a shared engine over differently prepared inputs is not parity. Apply each factor once. Never adjust lots and leave prices raw, or the reverse.

**Exception:** a reader that needs its own filter composes on `ledger_inputs.LOTS_SQL` and passes the rows to `ledger_inputs.prepare()`, as the MCP positions service does. The Manage page's trade-history table and the CSV importer read and write raw rows on purpose: they show and record what was entered. The source scan in `app/tests/test_ledger_inputs.py` keeps the list of such readers, with the reason next to each.

`compute_fifo_merged` / `compute_avg_cost_merged` then group the prepared rows by `(symbol, account)`, run the respective engine, and merge per symbol into a `DataFrame` with `symbol, qty, open_cost, avg_cost, realized_pnl`. Keep that output contract stable.

**Exception:** `corporate_actions.fetch_actions` returns an empty list only when the table does not exist (a database that never had the migration). Any other failure propagates: a transient error that read as "no actions" silently restated every figure into pre-split units, and a wrong cost basis is worse than an error.

**Rule:** the actions in effect are the ones that adjust anything.
- `fetch_actions` returns `corporate_actions.effective_actions()` of the recorded rows. The view `corporate_actions_in_effect` states the same rule in SQL.
- A split the collector recorded is pending: its `reviewed` flag is false. It changes no figure, prices or lots, until it is approved.
- Approve one with `python app/check_splits.py --approve SYMBOL:YYYY-MM-DD --shares-credited`, or with `--quantities-already-post-split`. `apply_schema.py` lists pending ones.
- A hand-entered row applies as entered. Two records of one split within seven days apply once, and a trigger refuses the second.
- Before recording a suspected split, confirm it against the vendor (`yfinance` `Ticker.splits`, as `check_splits.py` does). A price ratio by itself does not tell a split from a crash.

**Exception:** `fetch_recorded_actions` returns the whole register, pending rows included, for the scanners and reports that show it. It is not input for an adjuster.

**Rule:** the ledger is kept in US dollars, with no currency conversion. **Exception:** `--allow-non-usd` records a lot in another currency anyway, and the Manage page asks before recording one.
- `add_lot.py`, `sell_lot.py` and `import_csv_history.py` refuse a foreign listing suffix (`.TA`, `.L`, `.HK` …) or an instrument registered in another currency.
- `add_income.py` refuses income in another currency.
- Readers leave other-currency income and cash out of every total, and list them.
- An instrument in another currency stays in the valuation, flagged. See `portfolio_kpis.currency_warnings()` and `docs/methodology.md` §12.

### Dashboard (`app/streamlit_app.py`)
Two halves share one Streamlit session.
- The read-only views (Portfolio through History, Alerts & News) are a custom front end in an iframe (`st.iframe`). It is served from `app/dashboard/static/` (`shell.html`, `app.css`, `app.js`) and cannot write.
- Its data comes from `dashboard/payload.py`, with the queries in `dashboard/queries.py`.
- Manage, Advisor and Data Health are native Streamlit pages in `modern2_native.py`. Manage has tabs for trades, watchlist, cash, alerts, settings and export.
- The native pages write through `native_writes.py`, behind the gate in `native_auth.py`.
- Moving between the halves is a rerun in the same session, so an unlock and form drafts survive.
- The totals that the payload, the reports, the Advisor and the MCP share come from `app/portfolio_kpis.py`.
- `app.js` and `app.css` are generated and committed. `app/dashboard/static/build.mjs` bundles the JavaScript modules under `static/src/` with esbuild, and joins the fonts, `tokens.css` and `src/styles/app.css`.
- Edit the sources, then run `npm run build` from the repo root. Nothing builds at runtime, so the image and a host-run Streamlit need no Node.
- The `Front end` workflow (`.github/workflows/frontend.yml`) runs `npm run build:check`, ESLint with zero warnings, and the jsdom tests (`npm test`). The ESLint rules are what make an unescaped `innerHTML` a build failure.

### Layers
- `app/db.py` — psycopg2 connection + `fetch_all` / `execute` helpers. Everything in `app/` goes through this; the ad-hoc scripts at the repo root often open their own psycopg2 connections.
- `app/` — long-lived CLIs and modules (engines, dashboard, reports).
- Repo root — `Makefile` (the entry point for every command), `docker-compose.yml`, `docker/crontab` (collector) and `docker/crontab-llm` (brief, enrichment). Gitignored and host-specific: `run_*.ps1` / `setup_*.ps1` launchers and throwaway scripts under `archive/`. Prefer adding durable logic under `app/`.
- `app/tests/` — pytest suite for everything importable from `app/` (engines, loader, reports, importer, dashboard payload); `app/mcp/tests/` for the MCP server, run from the repo root.

### Snapshot collection
`snapshot_prices.py` selects symbols with open quantity (`ledger_inputs.open_symbols`, the one net quantity) OR `watchlist=TRUE`, pulls last/bid/ask via `yfinance` (`Ticker.info` only — `fast_info` is deliberately unused: its keys are camelCase, so the old `fi.get("last_price")` always returned `None` and every symbol fell through to `info` anyway, and it exposes neither bid/ask nor the trade timestamp the staleness guard needs), and inserts with `ON CONFLICT DO NOTHING`. It refuses a zero, negative or NaN price and stores Yahoo's out-of-hours `0.0` bid/ask as NULL; vendor-reported splits go to `corporate_actions` pending review. A run holds an advisory lock (a manual run and the scheduled one cannot overlap) and a whole-run deadline (`PORTFOLIODB_COLLECTOR_DEADLINE_S`). It refuses to collect outside the configured collector window (`app/market_window.py`, settable from the Settings page) unless `--ignore-window` is passed, so every caller obeys one rule. The `scheduler` service (supercronic, `docker/crontab`) is what invokes it on a schedule — see `docs/scheduling.md`. That service receives an env allowlist instead of `.env`, so it holds no API key. Keyed jobs (the brief, the Financial Datasets enrichment) go in `docker/crontab-llm`. The `scheduler-llm` service runs them with `.env`. When the collector starts reading a new setting, add it to the service's `environment:` in `docker-compose.yml`. Add it to `COLLECTOR_ALLOWLIST` in `tools/tests/test_compose_overrides.py` too.

### Image stages
`app/Dockerfile` has three stages: `base`, then `dev` (keeps pip), then `runtime` (pip removed).
`runtime` is the last stage, so it is the default target.
Every service and every published image runs `runtime`.
`make lock` and `make test` use the dev overlay's `devtools` service, which builds `dev`.
A running container has no pip by design. To add a package, edit `app/requirements.txt` and run `make lock`.

## Conventions

- Monetary math uses `Decimal` end-to-end inside the engines; conversion to `float` happens only when writing to a DataFrame for display.
- Symbols are stored and compared uppercase; CLIs uppercase user input before querying.
- Throwaway/incident scripts live under `archive/`; none hardcode the DB password (verified 2026-07-18). Keep it that way — always use `db.load_config` for credentials in new code, or `db.load_target()` plus the process's own role when it must not hold the application's login (as the MCP pool does).
