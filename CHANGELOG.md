# Changelog

This file records anything that changes **how a number is computed**, how data is
stored, or what an upgrade requires — because the README tells you to upgrade with
`docker compose pull`, and you are entitled to know what that changes before you
run it against your own records.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions follow [semver](https://semver.org/). **Read the entry before you
upgrade whatever the bump size** — a migration or a computation change can arrive
in a *minor*, and one already has: 1.1.0 added
`sql/migrations/002_market_benchmarks.sql`. The compose default floats the major
line (`:1`), so `docker compose pull` crosses a minor boundary on its own. A
major means something the upgrade cannot do for you at all. Every entry that
needs a schema step says so under **Upgrading**.

## [Unreleased]

### Changed

- **This file's own header said only a major bump carries a migration.** It did
  not: 1.1.0 was a minor and added `sql/migrations/002_market_benchmarks.sql`.
  Under `:1.0` the contradiction never reached anyone, because a default install
  could not cross a minor. Floating the default to `:1` in 1.1.3 makes it
  reachable — a reader who took the header at face value would pull a minor, skip
  the schema step, and land on the traceback 1.1.1 turned into a legible error.
  The header now says to read the entry whatever the bump size, and the 1.1.0
  entry carries a dated correction rather than a rewrite.
- **An entry may name a version; it may not say what a floating tag *currently*
  resolves to.** The 1.1.3 entry said `:1` "resolves to the current 1.1.2" — true
  when it was written into `[Unreleased]`, and falsified by the release it was cut
  into. `CHANGELOG.md` is exempt from CI's pin grep on purpose, because a
  changelog must name versions; this is the narrower rule that survives that
  exemption. The sentence now names the 1.1 line, which does not move.

## [1.1.3] — 2026-08-15

### Fixed

- **A fresh `docker compose up` installed the 1.0 line, not 1.1.** The compose
  default was `ghcr.io/amosgeva/portfoliodb:1.0`, which resolved to 1.0.3 — so
  anyone following the quick start got an image without the Markets strip, while
  the README's "What it does" list described it. It is now `:1`, which resolves
  to the current 1.1 line.
  - Nothing about `:1.0` was deliberate: the comment above it in compose, and
    both sentences about it in the README, all described major-line behaviour
    ("never a surprise major") — which is `:1`. Only the value said otherwise.
  - **This was not self-correcting.** `docker compose pull`, the upgrade this
    repo prints, cannot cross a minor boundary, so an install pinned at `:1.0`
    would have stayed on 1.0.x indefinitely.
  - **If you already installed from `:1.0`**, you are on the 1.0 line, whatever
    it last shipped — 1.0.3 as of this release. Pull this compose file (or set
    `PORTFOLIODB_IMAGE=ghcr.io/amosgeva/portfoliodb:1` in `.env`),
    then `docker compose pull && docker compose up -d` and run
    `apply_schema.py` — 1.1.0 added `sql/migrations/002_market_benchmarks.sql`,
    and skipping it is what produced the traceback fixed in 1.1.1.

### Changed

- **CI now rejects a written-down *minor* pin, not just a patch pin.** The patch
  guard added in 1.0.3 would never have caught `:1.0`, because `:1.0` is not a
  patch pin — it is a slower version of the same rot, and it outlived two minor
  releases unnoticed. Only the bare major may be written down now.

### Upgrading

**The compose file is fetched from `main`, so this fix is already live for a new
install** — it does not wait on this release. Re-download `docker-compose.yml` if
you installed before it, or set `PORTFOLIODB_IMAGE=ghcr.io/amosgeva/portfoliodb:1`
in `.env`. No migration in this release; if you are crossing 1.0.x → 1.1.x, run
`apply_schema.py` afterwards for 1.1.0's migration.

## [1.1.2] — 2026-08-15

**The application image is unchanged in substance** — nothing in this release
ships inside it. It is a repo-surface release: a link, and a guard.

### Added

- **A scheduled check that the marketing site does not contradict this repo.**
  CI already refuses hand-maintained test counts and hand-written patch pins in
  `README.md` and `docs/*.md`, because both went stale repeatedly. That guard
  stopped at the repo boundary, and `portfoliodb.app` — a separate project — was
  the surface a stranger reads *first*. It now gets checked daily against the
  **published page**, not a source tree, because merged is not deployed.
  - It matches rendered text rather than markup, so a claim cannot hide inside a
    tag: `<strong>494</strong> tests` is caught where a raw grep misses it.
  - It accepts a floor and rejects a precise count — `500+ tests` and
    `over 500 tests` pass, `494 tests` fails.
  - It never runs on a push or a pull request. The site is a network resource on
    someone else's deploy cadence, and an outage exits 2 rather than reporting a
    drift finding that is not one.
- **A link to `portfoliodb.app` on the README's first screen.** The GitHub
  `homepage` field is invisible on mobile, in a terminal, on the package page and
  in any fork.

## [1.1.1] — 2026-08-14

### Fixed

- **The dashboard had two scrollbars.** An outer one with a short range sat next
  to the real one inside the app frame. `fitViewport` shrank the iframe to the
  viewport but not the element container Streamlit wraps it in, which still
  reserved the server-side `height=` — so the page scrolled that difference. It
  had to be set as **`flex-basis`**: the container is a flex item with
  `flex: 0 0 <height>`, so an inline `height` is silently ignored. Fixed in both
  the app shell and the loading skeleton.
- **Benchmarks are no longer collected while their market is shut.** The vendor
  keeps serving the last print when a futures market closes, so a 15-minute
  collector wrote that same price under a fresh timestamp ~96 times a day: the
  Markets strip's "as of" line claimed a Friday price was current, and the
  sparkline grew a flat tail that read as a quiet market rather than a closed one.
  Holdings are deliberately unaffected — outside regular hours their
  `regularMarketPrice` legitimately *is* the previous close, and refusing it would
  leave the portfolio unpriced every evening.
- **Skipping the schema step after upgrading to 1.1.0 produced a psycopg2
  traceback** in the scheduler log. It now logs one line naming the missing
  column, the exact command that fixes it, and the fact that nothing else is
  affected — and the job exits non-zero, because a cron job that exits 0 on
  failure is one nobody notices.

### Upgrading

No migration in this release — if you already ran the schema step for 1.1.0 there
is nothing to do beyond pulling:

```bash
docker compose pull && docker compose up -d
```

## [1.1.0] — 2026-08-14

### Added

- **A Markets strip** on the portfolio view: index futures and volatility, so the
  dashboard says something during the hours your own holdings have no prints.
  Futures quote nearly 23 hours; a pre-market equity quote frequently does not
  exist at all, which is why this answers "what is happening before the open" and
  extended-hours equity prices would not.
  - Symbols come from a setting (`market_overview_symbols`, editable in
    Manage → Settings) as `SYMBOL:Label`. Any yfinance symbol works. Clearing the
    field hides the strip.
  - Collected by `snapshot_prices.py --benchmarks` every 15 minutes, Sun–Fri,
    **ignoring the collector window** — the window describes when *your* market
    trades, and gating futures on it would leave the strip nine hours stale at
    exactly the hour you look.
  - **Benchmarks cannot reach the portfolio.** They have no lots, so the P&L
    engines never see them; a new `instruments.benchmark` flag keeps them out of
    the watchlist rail and Data Health's per-symbol scope; and a benchmark run
    writes **no** row to `snapshot_runs`, so the MCP cutoff and Data Health
    freshness keep meaning "the portfolio's prices" rather than "a futures fetch".
  - A symbol with no history yet reads "no data yet" rather than 0.00%.

### Upgrading

**This release adds a column, so run the schema step after pulling:**

```bash
docker compose pull && docker compose up -d
docker compose run --rm dashboard python app/apply_schema.py   # idempotent
```

Skip it and nothing breaks loudly — the strip simply stays empty and the
benchmark job logs an error about a missing column. Everything else is unaffected.

**The compose default pins `:1.0`, which floats across patches but not across
minor versions**, so a default install does *not* pick this up automatically.
That is deliberate — no surprise features — and it means changing
`PORTFOLIODB_IMAGE` to `ghcr.io/amosgeva/portfoliodb:1.1` (or `:1`, which floats
across minors within 1.x).

> **Correction, 2026-08-15.** The paragraph above is left as written because it
> is what this release shipped, but two of its claims are no longer true. The
> compose default is now `:1`, so a default install *does* pick this up — see
> [1.1.3]. And "that is deliberate" was wrong when written: nothing chose
> `:1.0` over `:1`; the surrounding prose described the major line and only the
> value disagreed.

## [1.0.3] — 2026-08-14

No change to how any number is computed.

### Changed

- **The disclaimer now names the regime it disclaims under.** Reviewed wording:
  the author is not a licensed investment adviser, investment marketer or
  portfolio manager under Israeli law (Regulation of Investment Advice,
  Investment Marketing and Investment Portfolio Management Law, 5755-1995) or any
  other jurisdiction, and nothing here substitutes for personal advice from a
  licensed professional who knows your circumstances.
- **The numbers are stated as informational, not tax figures.** Cost basis,
  realized P&L and returns are computed for portfolio tracking. They are not
  prepared under any tax authority's rules — lot-matching method, currency
  conversion dates and inflation adjustment may all differ — so they need
  independent verification before they go anywhere near a tax return.
- **The warranty disclaimer is surfaced in the README** rather than left to
  whoever opens `LICENSE`.
- **The Advisor tab and the executive report carry the short form.** The report
  especially: it is a standalone HTML file that travels without the app or the
  README, so the caveat had to travel inside it.

## [1.0.2] — 2026-08-14

No change to how any number is computed. Upgrading is optional.

### Added

- **The AGPL text now ships inside the image.** The image declared
  `org.opencontainers.image.licenses=AGPL-3.0-or-later` while the licence itself
  was not in it, so anyone who pulled the image without visiting the repository
  had a label and nothing to read. It is at `/app/LICENSE`; the source it
  corresponds to is the `org.opencontainers.image.source` label.

### Changed

- **Scope stated plainly, in the README and on the Advisor page.** This keeps a
  record of assets you already hold, across every broker you use. It is not a
  broker, a custodian or an adviser and replaces none of them: it holds no money,
  connects to no broker, and places no orders. The optional advisor runs on *your*
  model — your key, or a local model on your machine — reading a one-pager *you*
  wrote, so the question it answers is whether your holdings still match rules you
  set for yourself. Any suggestion in its output is your model's text, and you are
  the only decision-maker.
- **The several-brokers-one-record behaviour is documented** rather than implied
  by a CLI flag. Matching is scoped per `(symbol, account)`, so each broker's cost
  basis stays its own — shares bought at one broker are never FIFO-matched against
  a sale at another — while the dashboard shows per-account and merged views.

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

[Unreleased]: https://github.com/amosgeva/PortfolioDB/compare/v1.1.3...HEAD
[1.1.3]: https://github.com/amosgeva/PortfolioDB/releases/tag/v1.1.3
[1.1.2]: https://github.com/amosgeva/PortfolioDB/releases/tag/v1.1.2
[1.1.1]: https://github.com/amosgeva/PortfolioDB/releases/tag/v1.1.1
[1.1.0]: https://github.com/amosgeva/PortfolioDB/releases/tag/v1.1.0
[1.0.3]: https://github.com/amosgeva/PortfolioDB/releases/tag/v1.0.3
[1.0.2]: https://github.com/amosgeva/PortfolioDB/releases/tag/v1.0.2
[1.0.1]: https://github.com/amosgeva/PortfolioDB/releases/tag/v1.0.1
[1.0.0]: https://github.com/amosgeva/PortfolioDB/releases/tag/v1.0.0
