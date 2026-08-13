# Changelog

This file records anything that changes **how a number is computed**, how data is
stored, or what an upgrade requires — because the README tells you to upgrade with
`docker compose pull`, and you are entitled to know what that changes before you
run it against your own records.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions follow [semver](https://semver.org/): a major bump means a migration or
a computation change you should read before applying.

## [Unreleased]

Nothing yet.

## [1.0.1] — 2026-08-13

### Fixed

- **CSV import could not record a sale.** `side` was hardcoded to `BUY`, so
  importing a trade history recorded every sale as another purchase: open
  quantity too high, realized P&L reported as zero, cost basis wrong — and
  nothing errored. An optional **`Side`** column now carries `BUY`/`SELL`
  (absent still means BUY, so holdings exports import unchanged), a negative
  quantity is **refused rather than assumed to be a sale**, and the import
  summary states the BUY/SELL split so a mismatch is visible immediately.
  **If you imported a history containing sales before this, re-import it** —
  the duplicate guard now includes `side`, so the missing sells will land.
- **A failed database connection is now explained instead of dumped.** Stale
  `PORTFOLIODB_MCP_RO_*` credentials took out the whole Data Health page with
  psycopg2's raw text, container IP included. One explainer now serves both that
  page and `/healthz`: it names the role, says how to fix it (`make ro-role`) and
  how to opt out (clear both keys).
- **The documented version pin was wrong.** The README and `docker-compose.yml`
  told you to pin `:v1.0.0`, which does not exist — image tags carry no `v`. The
  real tags are `1.0.0`, `1.0` and `1`.

### Added

- `server.json` and an `io.modelcontextprotocol.server.name` image label, so the
  MCP server can be published to the MCP Server Registry. **This is the first
  release whose image carries that label**; 1.0.0's does not.
- A CSV mapping recipe in `docs/csv-import.md` for turning any broker export into
  the expected columns, and a sample file that now demonstrates a partial sale.

## [1.0.0] — 2026-08-13

First public release. Everything below already existed; this is the starting
point rather than a list of changes.

### The ledger

- Append-only lot table: every BUY and SELL is a row, and open quantity, cost
  basis and realized P&L are **recomputed from those rows on every read**. No
  derived number is ever persisted, so no number can quietly drift from the
  trades that produced it.
- FIFO and moving-average engines run side by side over the same lots, scoped per
  `(symbol, account)`. BUY fees inflate cost basis; SELL fees reduce proceeds.
- Splits are applied at read time from `corporate_actions`, so an adjustment is
  undone by deleting a row.
- Money is `Decimal` end to end inside the engines; conversion to `float` happens
  only for display.

### Running it

- One published multi-arch image (`linux/amd64`, `linux/arm64`) — pulled, not
  built. No Python or toolchain on your machine.
- Dashboard, scheduler and an optional read-only MCP server all run from that one
  image; `make init && make up && make schema` is the whole install.
- Price collection obeys a single configured market window, editable from
  Manage → Settings rather than in `.env`.
- Backups are a `pg_dump` (`make backup`); restore refuses to run into a
  non-empty database.

### The optional parts

- **Advisor** — brief and chat against Anthropic, OpenAI, OpenRouter or a fully
  local model through Ollama. It reads an investor one-pager you write yourself,
  pasted into the dashboard and stored in the database.
- **MCP server** — 47 tools, 7 resources and 7 prompts over the same engines the
  dashboard uses. Read-only: the pool forces `default_transaction_read_only`, and
  a SELECT-only role is available via `make ro-role`.

### Known limitations, stated plainly

Single currency (mixed currencies are **wrong, not approximate**), equities and
ETFs only, no broker sync, no authentication, no shorts, one person's portfolio.
See "Scope and limitations" in the README before installing.

[Unreleased]: https://github.com/amosgeva/PortfolioDB/compare/v1.0.1...HEAD
[1.0.1]: https://github.com/amosgeva/PortfolioDB/releases/tag/v1.0.1
[1.0.0]: https://github.com/amosgeva/PortfolioDB/releases/tag/v1.0.0
