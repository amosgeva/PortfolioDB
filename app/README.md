# app/

The Python side of PortfolioDB: the CLIs, the engines, the dashboard, the
reports and the MCP server. Installing and running the project is in the
[root README](../README.md); every command is a `make` target, and
[docs/commands.md](../docs/commands.md) gives the `docker compose` line behind
each one. How each number is computed is in
[docs/methodology.md](../docs/methodology.md).

## Where things are

| Area | Modules |
|---|---|
| Ledger core | `ledger_core.py` (the `Lot` class, processing order), `ledger_inputs.py` (lots and prices restated by the corporate actions in effect: what every reader loads), `holdings.py` (the one net-quantity implementation), `fifo.py` / `avg_cost.py` (the engines), `portfolio.py` (merge per symbol), `corporate_actions.py`, `oversell.py` |
| Figures | `portfolio_kpis.py` (the totals every surface shares), `twr.py`, `xirr.py`, `period_stats.py`, `benchmark.py` |
| Writers | `add_lot.py`, `sell_lot.py`, `set_cash.py`, `set_watchlist.py`, `add_income.py`, `import_csv_history.py`; the lot writers and the Manage page share `ledger_writes.py` (duplicate and currency-scope checks) |
| Collection | `snapshot_prices.py` (prices, within `market_window.py`), `check_splits.py` (split review), `enrich_instruments.py`, `fd_weekly_enrichment.py` / `fd_store.py`, `fetch_ticker_logos.py` |
| Dashboard | `streamlit_app.py` (the shell); `dashboard/` (the read-only views' payload, SQL and static front end); `modern2_native.py` (Manage, Advisor, Data Health); `native_auth.py` (the write-page password); `native_writes.py` |
| Reports and advisor | `report_portfolio_db.py`, `report_weekly_db.py`, `exec_report*.py`, `advisor.py`, `llm.py` |
| Plumbing | `db.py` (connections and credentials), `settings.py` (DB → env → default), `apply_schema.py` (schema and migrations), `create_ro_role.py` (the MCP's read-only role), `demo_seed.py` |
| MCP server | `mcp/` — see the [MCP section](../README.md#mcp-server) of the root README |

`Dockerfile` builds the one image every service runs (dashboard, both
schedulers, MCP server and the CLIs). Its last stage, `runtime`, is what runs
and ships, and has no pip; the `dev` stage keeps pip for `make lock` and
`make test`.

## Running from here

Inside the stack: `make positions`, `make add-lot ARGS="…"` and so on. On the
host, with the dependencies installed from `requirements.txt` (a repo-local
`.venv` is the tidy place for them) and `PORTFOLIODB_PASSWORD` set, run the
scripts from this directory — they import each other as bare modules:

```bash
cd app
python positions.py
streamlit run streamlit_app.py --server.address 127.0.0.1 --browser.gatherUsageStats false
```

Keep both flags on a host run. Without `--server.address` Streamlit listens on
every interface — a host process has no loopback-only port mapping in front of
it, as the containers do — and without the second it sends Streamlit's usage
statistics, which the image turns off (`STREAMLIT_BROWSER_GATHER_USAGE_STATS=false`).
Reaching it from another machine is the same deliberate step as for the stack:
see [docs/exposure.md](../docs/exposure.md).

The tests: `pytest tests/` from here, and the MCP suite from the repo root
(`pytest app/mcp/tests/ -m "not slow"`) — see
[CONTRIBUTING.md](../CONTRIBUTING.md#tests) for why the two differ.
