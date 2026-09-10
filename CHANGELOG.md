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

### Fixed

- **The weekly and executive reports state splits in one unit basis.** The
  weekly report valued its start and end positions in each date's units but
  multiplied them by raw snapshot quotes, and fed raw trade rows into its
  contribution arithmetic, so a pure 2:1 split inside the week printed the
  holding as a 50% loser with a `qty Δ +10`. The executive report restated
  its lots and its EOD series but joined the raw latest quotes, so a symbol
  whose newest snapshot predated a recorded split was worth twice its value
  with the whole excess as unrealized profit. Both reports now prepare one
  ledger and pass every quote and every week trade through it; the weekly
  report lists the week's corporate actions in their own block, and the
  trades block still shows what was entered. A pure split contributes $0
  and changes neither value nor P&L (1.7.2 re-audit, F03 remaining cases).

## [1.7.2] — 2026-09-10

The second pass over the 1.7.x audit: six findings the re-audit of 1.7.1
turned up, each a case the first pass had reasoned about and got wrong at
one edge — a split dated after the observation, a dividend paid before a
later split, a liquidated stretch on the chart, an unnamed account beside a
named one, a half-filled CSV trade row, and the write password sitting in the
one container built not to have it. No schema change and no migration.

**Upgrading**

- If you ran `add_income.py --backfill` on 1.7.0 or 1.7.1 for a symbol that
  split *after* one of its dividends, the estimates for the earlier dividends
  are undercounted. Rerun with `--backfill --replace-estimates` for that
  symbol; it deletes the `source='yfinance'` rows first and says how many it
  replaced. Manual income rows are never touched.
- Compose deployments that run the MCP server on
  `PORTFOLIODB_MCP_ALLOW_RW_FALLBACK=1` must now add `PORTFOLIODB_PASSWORD`
  to the `mcp` service in `docker-compose.override.yml`
  ([exposure](docs/exposure.md#the-mcp-server) shows the block). Deployments
  on the read-only role, which is the default, need nothing.
- A CSV export with half-filled trade rows that imported clean before will
  now be rejected with the line numbers; fix the rows or blank all three
  trade fields to make them quote-only.

### Security

- **The MCP container no longer receives the application's read-write
  password.** The `mcp` compose service inherited the shared environment
  block, and with it `PORTFOLIODB_PASSWORD`, because the pool read the
  database address through `load_config()`, which insists on that password —
  so the one container built to hold only a `SELECT`-only role also held the
  login that could write. The address now comes from a credential-free
  `db.load_target()`, the compose file gives `mcp` the address and its own
  settings only, and the read-only path never looks at the write password.
  The deliberate `PORTFOLIODB_MCP_ALLOW_RW_FALLBACK=1` opt-out still works but
  you now supply `PORTFOLIODB_PASSWORD` to the service yourself in
  `docker-compose.override.yml` ([exposure](docs/exposure.md#the-mcp-server)
  shows the block); without it the server refuses to start and says so.

### Fixed

- **CSV import rejects incomplete trade rows instead of skipping them.** A
  row with some of `Trade Date`, `Purchase Price` and `Quantity` filled in
  was treated as "not a lot": nothing was imported for the trade, its price
  snapshot still went in, and the run reported zero rejections — a trade
  missing from the ledger behind a clean import. Such a row is now rejected
  with its line number and the missing field, and so is a trade row with no
  symbol; a row with all three trade fields blank is still a quote-only row,
  and a wholly blank line is still skipped. The transaction policy applies:
  atomic mode writes nothing from that file, `--continue-on-error` counts the
  row as rejected and writes the rest.

- **The weekly report no longer crashes when an unnamed account meets a named
  one.** 1.7.0 moved its position aggregation into Python and sorted
  `(account, symbol)` keys with the default ordering, which refuses to compare
  `None` with a string; any ledger with both an account-less lot and a named
  account stopped the report before it printed. Unnamed accounts now sort
  first, and the stored value is kept as is, so `None` and an empty-string
  account stay distinct.

- **The portfolio-value chart keeps the days on which everything was sold.**
  The history dropped every zero-valued point, so a liquidated stretch
  vanished from the chart and the line bridged from the last funded day to
  the re-entry. Only the points before anything was ever held are dropped
  now; a zero after that is drawn as a flat line at zero, and a range that
  begins on such a day shows no percentage change rather than a division by
  zero.

- **Dividend backfill: a dividend paid before a later split is no longer
  undercounted.** yfinance states every historical per-share dividend in
  today's split-adjusted units (Apple's $0.82 of August 2020 comes back as
  $0.205 after the 4:1), so the shares it is multiplied by must be in today's
  units too. 1.7.0 read the ledger as of the ex-date, which left a later split
  out and recorded half (or a quarter) of the cash for every dividend paid
  before one. The backfill now selects lots by ex-date and counts them in
  current units (`ledger_inputs.load(units="current")`). **Existing
  estimates:** rows written by earlier backfills carry the old amounts, and
  because the dedupe key includes the amount a rerun would insert the
  corrected row beside the old one. `add_income.py --backfill
  --replace-estimates` deletes the symbol's `source='yfinance'` rows first
  and reports how many it replaced; manual rows are never touched.

- **Historical MCP positions and the daily/EOD report state splits in the
  right units.** `get_positions` with an `as_of` before a recorded split
  applied the split anyway, so "the day before a 2:1" reported twenty shares
  against the pre-split quote — twice the value that existed; a stale quote
  observed before an ex-date the cutoff was past was joined to restated
  shares the same way. The daily/EOD report restated its lots but compared
  raw previous, day-start and current quotes, so a split between two quotes
  printed a loss of the whole ratio (`Delta: $-1,000.00` on a pure 2:1 with
  nothing else moving). Actions now apply only when dated on or before the
  observation date (`ledger_inputs.prepare(as_of=…)`, shared by every
  date-filtered reader), stale quotes are restated into the cutoff's units,
  and the report's three comparison quotes go through the prepared ledger
  with their own timestamps. Installs without a `corporate_actions` row see
  no change.

## [1.7.1] — 2026-09-10

A one-line fix for a 1.7.0 regression that broke every dashboard page. No
schema change and no migration; the 1.7.0 **Upgrading** steps still apply if
you have not done them.

### Fixed

- **1.7.0's dashboard failed to load with "too many values to unpack
  (expected 2)".** The news feed's normal path still returned a bare list
  after its contract changed to `(rows, problem)` in 1.7.0; the payload test
  had stubbed the whole function and so never ran that line. The test now
  stubs only the news store, so the section's real code runs. Anyone on 1.7.0
  sees the error banner on every page; 1.7.1 is the fix.

## [1.7.0] — 2026-09-09

The release that acts on the 2026-09-09 codebase audit: every High and Medium
finding, and the routine items behind them. Read **Upgrading** before pulling —
this one has a migration, a new requirement for the MCP server, and several
figures that change on purpose.

Three things move numbers. Recorded splits now reach the dashboard, the CLI and
the reports, not only the MCP tools. Time-weighted return values a sale at its
own price, so closing a position no longer reads as 0% or −100%. Portfolio
drawdown is measured on the flow-adjusted growth curve, so a withdrawal is no
longer a drawdown. Each has its own entry below with the before and after.

Minor rather than major because every upgrade step has a documented path the
operator can take (`make schema`; `make ro-role` or the explicit fallback
flag; two lines moved to `.env`), and nothing about the ledger's data has to be
rewritten. It is the largest minor this project has shipped, and the entries
are long because you are entitled to know what changes before you run it
against your own records.

### Added

- **A sale bigger than the position is warned about when it is entered.**
  `sell-lot`, `add-lot --side SELL` and the CSV importer check the prepared
  ledger for the account as of the trade date and print what is held versus
  what is being sold, then still record the row: the usual cause is the wrong
  account, a mistyped date or a missing BUY, and the row is the evidence. The
  FIFO engine's read-time truncation warning stays; this is the same fact,
  said to the person typing. The importer counts these as `oversells`.
- **`get_data_quality` reports an instrument whose registered currency is
  not the reporting currency** (`foreign_currency`, a correctness issue at
  any position size): the ledger sums face values and nothing converts, so a
  EUR position in a USD ledger makes every total wrong. Previously nothing
  checked.

### Security

- **Symbols reach the page as text and the disk only when they look like
  symbols.** The dashboard's two symbol `<select>`s interpolated symbols into
  `innerHTML` unescaped; they are built with the DOM `Option` constructor
  now. The logo cache turned any symbol into `<symbol>.png` under
  `app/dashboard/static/logos/`, writing (the fetcher) and reading back into
  the page as a data URI (the dashboard); both go through one allowlist that
  admits what real symbols look like (`BRK.B`, `^GSPC`, `ES=F`, `0700.HK`) and
  nothing with a path separator. A symbol is operator-entered text — the
  importer and `add-lot` accept any string — so it is treated as such.

- **The published image is reproducible and scanned.** `app/requirements.txt`
  is ranges; the image resolved them afresh on every build, so a passing test
  run said nothing about the image a user pulled a week later, and the base
  `python:3.14-slim` tag moved underneath it. Now `app/constraints.txt` holds
  the exact set the image was tested with (generated inside the image with
  `make lock`), the Dockerfile installs with it, CI fails if the image's
  `pip freeze` differs from the file, the base image is pinned by digest with
  Dependabot proposing bumps for it and for the pinned actions, and a weekly
  workflow runs `pip-audit` against the lock and Trivy against the built
  image (fixable HIGH/CRITICAL findings fail it). Provenance attestations stay
  off, with the reason recorded next to the setting. No runtime change: the
  pins are what the last green build already contained.

- **The MCP server requires its read-only database role and receives only the
  environment it needs.** The pool fell back to the application's read-write
  credentials silently when `PORTFOLIODB_MCP_RO_USER` was unset, protected
  only by `default_transaction_read_only=on` — a session setting any
  statement can switch off. It now refuses to connect without the role and
  says how to create it; the fallback survives as an explicit, logged opt-out
  (`PORTFOLIODB_MCP_ALLOW_RW_FALLBACK=1`). In compose the `mcp` service no
  longer inherits the whole `.env`: it gets the database settings and its own
  keys, not the LLM keys or the vendor API key. **Upgrading: if you run the
  MCP server and never ran `make ro-role`, run it now and add the two lines
  it prints to `.env`, or set the fallback flag.** Installs that already set
  the role see no change. A live test now proves the role refuses a write on
  privilege even inside a `READ WRITE` transaction.

- **The advisor's base URL is read from `LLM_BASE_URL` in `.env` only; the
  Settings-page field is gone.** The page has no login, and the base URL is
  where the OpenAI-compatible client sends the API key: anyone who could open
  the dashboard could point `openai` at a server they controlled, trigger a
  brief, and receive `OPENAI_API_KEY` as a Bearer header. A URL from an
  unauthenticated form must never decide where a secret goes. In addition a
  vendor's named key (`OPENAI_API_KEY`, `OPENROUTER_API_KEY`) now only travels
  to that vendor's own origin — an `LLM_BASE_URL` that points the provider
  elsewhere sends the generic `LLM_API_KEY`, or fails with a message naming
  the two variables. **Upgrading:** if you had set a base URL on the Settings
  page (Ollama on the Docker host is the common case), put the same value in
  `.env` as `LLM_BASE_URL=…` and restart. The old row is ignored, logged once,
  and deleted the next time Settings is saved.

- **The MCP server's unauthenticated `/healthz` now answers only `ok`, `db`
  and `last_snapshot_age_s`.** It used to return the full `get_health`
  payload: the last collector run's `error` text verbatim — which names the
  symbols that failed and carries a traceback when yfinance raised — plus
  per-table enrichment counts and the database's own failure reason (role
  and container IP). Anyone who could reach the port learned part of the
  portfolio universe without a token. The full diagnostic is unchanged behind
  the bearer token as the `get_health` tool. Monitors that parsed the old
  body need the new three keys; the status code contract (200 up, 503 down)
  is the same, so the compose healthcheck is unaffected.

- **The "localhost only" and "stop publishing Postgres" overrides in
  `docs/exposure.md` now actually do that.** Both examples were plain lists,
  and Compose merges an override's `ports` into the base file's list rather
  than replacing it, keying entries on host IP as well as port — so the
  loopback mapping landed *beside* the inherited `0.0.0.0:8501`, and
  `ports: []` removed nothing. An operator who followed the guide had a
  dashboard that was still open to the network while their override said
  otherwise. The examples and `docker-compose.override.yml.example` now use
  `ports: !override` and `ports: !reset []` (Compose 2.24.4+), and a test
  renders every documented override and fails if a wildcard mapping survives.
  **If you copied the old example, re-copy it** and check with
  `docker compose config` that no `0.0.0.0` entry remains. Nothing about the
  base file's defaults changed.

- **A source build from a populated working tree no longer bakes `app/.env`
  into the image.** `.dockerignore` excluded the root `.env` and nothing else,
  while the Dockerfile copies the whole `app/` tree — so an operator who kept
  the host-Python sidecar `app/.env` (database password, MCP token, vendor API
  key) and ran `docker compose build` got an image with the file in it. Git's
  ignore rules never applied to a Docker build context. Every `.env` sidecar
  is now excluded recursively, along with the cached ticker logos, backups and
  `docs/internal/`; CI plants fake secrets in those places and fails if any
  reaches a layer. The published `ghcr.io` image was never affected: it is
  built from a clean checkout. Only operators who built and *shared* a local
  image need to consider that image compromised.

### Changed

- **Portfolio drawdown is measured on the time-weighted growth curve, not on
  market value.** `get_drawdown_stats` for the whole portfolio ran on the
  market-value series, which falls when securities are sold and rises when
  money is added — a withdrawal read as a drawdown and a deposit could hide
  one. It now runs on the same daily flow-adjusted curve as the period
  returns, says so in a new `basis` field (`twr_growth_curve`; the review
  snapshot carries it as `risk.drawdown_basis`), and its `peak`/`trough` are
  index levels with 1.0 at the first observation rather than dollars. A single
  symbol's drawdown is unchanged (`basis: "price"`), and
  `holdings_basis="current_constant"` keeps the old market-value definition
  for comparison. **The portfolio's max and current drawdown figures change
  for any ledger with sales or purchases in its history.**
- **The dashboard names a section it could not load instead of rendering it
  empty.** The market strip and the news feed swallowed a failed query into an
  empty list; the payload now carries a `degraded` list and the page shows a
  banner naming the section and the error type.

- **Every number written to the ledger must be finite, in range, and the
  right sign — and the database now refuses NaN too.** The CSV importer and
  the write CLIs (`add-lot`, `sell-lot`, `set-cash`, `add_income`) parsed
  quantities, prices, fees and amounts with `float()`, which accepts `NaN`
  and `Infinity`. NaN is neither `< 0` nor `== 0`, so it passed the
  importer's checks, and PostgreSQL's numeric NaN sorts above every finite
  value, so it passed `CHECK (quantity > 0)` and was stored. Infinity failed
  later at the column type, aborting the importer's transaction. One parser
  (`app/ledger_numbers.py`) now handles all of them: exact `Decimal`, finite,
  within the twelve integer digits `NUMERIC(20,8)` holds, sign per column.
  A `Current Price` that is not finite imports as "no price". **Upgrading:**
  run `make schema` (or `docker compose run --rm dashboard python
  app/apply_schema.py`). Migration `003_finite_numeric_checks.sql` adds a
  `<> 'NaN'` check to every numeric ledger column; it fails on purpose if a
  NaN is already stored, which is a defect to look at rather than one to
  keep. Schema version is now 0.4.

- **CSV import: a file goes in whole, or not at all.** `import_csv_history.py`
  committed every insert on its own and carried on after a database error, so
  "Rows failed: 1" could mean the earlier rows were already in (plus the
  instruments they referenced) and every row after the failure had been
  reported as an error of its own, because a failed statement leaves a
  PostgreSQL transaction aborted. A parse failure ended the run with exit 0.
  Now every row is validated before anything is written, a clean file is
  written in one transaction, and a row PostgreSQL rejects rolls the whole
  file back. `--continue-on-error` is the explicit partial mode: valid rows go
  in under per-row savepoints, rejected ones are listed, and the run exits 2.
  Exit codes are 0 (all in), 1 (something rejected and not written, no files,
  refused pattern), 2 (partial). The summary distinguishes inserted from
  already-present rows for lots and snapshots. See
  `docs/csv-import.md#errors-transactions-and-exit-codes`. **Scripts that
  wrapped the importer and ignored its exit status now see a nonzero code
  when something was rejected.**

- **Recorded splits now reach the dashboard, the positions CLI, both reports
  and the dividend backfill — not only the MCP tools.** The FIFO engine was
  shared, but the *inputs* were not: the MCP services restated lots and
  prices through `corporate_actions` before running it, while every other
  surface fed it raw rows. Ten shares bought at 100 with a recorded 2:1 split
  and a 50 quote read as twenty shares worth 1,000 and a 0% return on the MCP
  side, and as ten shares with a −50% day on the dashboard and in the reports.
  A new `app/ledger_inputs.py` is now the one place lots are read and
  restated, and hands each reader the matching price adjusters; every surface
  goes through it. **If you have a row in `corporate_actions`, the dashboard's
  share counts, average cost, sparklines, value history and returns change to
  agree with the MCP tools.** Installs without one see no change.

- **The dashboard's portfolio-value chart is now actual history.** It
  multiplied *today's* share counts by past prices, so a position bought last
  month appeared to have been held all year, a sold one vanished from the
  record, and buying more rewrote the past. It now values the holdings
  actually held on each snapshot day, with prices carried forward across a
  symbol's missing quotes — the same reconstruction the MCP
  `portfolio_value_history` tool and the drawdown statistics already used.
  The risk block keeps its price-risk view of the *current* basket over
  historical closes and now says so in its payload (`risk.basis`) as well as
  in its caption.

- **Dividend backfill counts entitlement in split-adjusted shares.**
  `add_income.py --backfill` summed raw BUY−SELL quantities, so ten pre-split
  shares were ten for a dividend paid after a 2:1 and half the entitlement
  was recorded. It now reads the prepared ledger as of the ex-date, which is
  the unit yfinance states its per-share figures in. Rows say what they are
  in their `notes` (pay date = ex-date, entitlement estimated from the
  ledger), reruns report inserted versus duplicate counts, and a new
  `--per-account` flag writes one row per account instead of a merged row
  with a NULL account. The merged default is unchanged so existing backfilled
  rows keep deduplicating.

- **A failure to read `corporate_actions` is now an error, not "no
  actions".** Only a missing table (a database that never had the migration)
  still degrades to no adjustment; any other failure propagates instead of
  silently restating every figure into pre-split units.

- **Time-weighted return: a sale is now valued at its own price, and closing
  a position no longer produces 0% or −100%.** The daily sub-period formula
  netted a sale into the *denominator* as a negative flow, so a day that
  closed the position had a denominator of value-minus-proceeds: selling one
  share bought at 100 for 110 froze the growth factor (0% instead of +10%),
  and selling it for 90 multiplied the factor by zero (−100%, and every later
  period stuck there). A partial-sale day was overstated the same way — a +10%
  day with half the position sold read as +22%. Purchases are now weighted at
  the start of the day and sale proceeds at the end:
  `r = (MV + div + proceeds) / (MV_prev + purchases) − 1`. A day with nothing
  held and nothing bought carries the factor through unchanged, a period made
  only of such days reports `null` rather than 0%, and income that arrives
  after a position was sold is credited against the last capital that earned
  it. **Every TWR figure changes** on any day that had a sale — dashboard
  strip, `get_period_returns`, `get_benchmark_comparison`, volatility and the
  period statistics all read the same curve. Days with only purchases or no
  trades are unchanged. Records from `twr.build_daily_records` gain `inflow`
  and `outflow`; `flow` (the net) is still there. `docs/methodology.md` §4
  states the convention.

### Fixed

- **`backup` and `restore` can no longer report success after failing.** Both
  runners piped `pg_dump` into `gzip` and trusted the pipeline's exit status,
  which is the *last* command's: a `pg_dump` that died still handed `gzip` a
  valid, non-empty archive of nothing, the size check passed it, and the
  runner printed `wrote …`. `restore` ran `psql` with its defaults, which log
  a SQL error, carry on, and exit 0 — so a dump that failed halfway printed
  `restored` and a lots count. Now the dump runs under `pipefail`, the archive
  must decompress and carry `pg_dump`'s completion marker before it is renamed
  from `.part` to its final name, and restore runs `gzip -t` first and `psql
  -v ON_ERROR_STOP=1 --single-transaction`, so the first error aborts and
  rolls back, leaving the target as empty as the guard found it. One error
  the old defaults skipped and the new ones stop on is handled: a dump that
  carries its own `CREATE SCHEMA public;` (older `pg_dump` versions emit it
  when the schema was recreated) has the empty target schema dropped first,
  inside the same transaction. CI now injects a failing and a half-finished
  `pg_dump`, a truncated archive and a dump with a mid-file SQL error against
  both `make` and `pdb.ps1`. **Check
  the backups you already have** with `gzip -t` *and*
  `gunzip -c <file> | grep -c 'PostgreSQL database dump complete'`; an archive
  that passes the first and fails the second is empty.

### Upgrading

1. **Back up first.** The runners now verify what they write; the copy you
   already have may not have been verified — check it with `gzip -t` and
   `gunzip -c <file> | grep -c 'PostgreSQL database dump complete'`.
2. `docker compose pull && docker compose up -d`, then **`make schema`** (or
   `docker compose run --rm dashboard python app/apply_schema.py`). Migration
   `003_finite_numeric_checks.sql` adds the not-NaN constraints; schema version
   is 0.4. It fails on purpose if a NaN is already stored.
3. **If you run the MCP server:** it now requires the read-only role. Run
   `make ro-role` and paste the two lines it prints into `.env`, or set
   `PORTFOLIODB_MCP_ALLOW_RW_FALLBACK=1` to keep the old behaviour with a
   warning. Monitors that parsed the old `/healthz` body need its three new
   keys.
4. **If you set an advisor base URL on the Settings page** (Ollama on the
   Docker host is the usual case), put the same value in `.env` as
   `LLM_BASE_URL=…`; the page no longer has the field.
5. **If you copied the old `docker-compose.override.yml.example`** to bind to
   localhost, re-copy it: the old plain-list form left the wildcard binding in
   place. `docker compose config` should show no `0.0.0.0` entry.
6. **If you build the image yourself from a working tree that holds an
   `app/.env`,** rebuild: the old `.dockerignore` let it into the image.
7. Expect changed figures where the entries above say so: split-adjusted
   share counts and history on the dashboard, TWR on any day that had a sale,
   portfolio drawdown on any ledger with trades, and the value chart showing
   what was actually held. Scripts that wrapped the CSV importer now see a
   nonzero exit status when a row was rejected.

## [1.6.0] — 2026-09-09

The review snapshot gains a per-account cash breakdown, and the period
statistics stop holding back a finished week or month until the next one has a
snapshot. No schema and no migration — see **Upgrading**.

Minor rather than patch because a field was added to an MCP tool's output:
backward compatible, but something new for a client to consume, which a patch
should not carry.

### Added

- **`summary.cash_by_account` in `get_portfolio_review_snapshot`.** The
  per-account breakdown behind `summary.cash` — the latest balance each account
  had entered at the cutoff — was computed on every call and then discarded. It
  is now returned next to the total it explains, so a reader can see which
  account holds the cash. Additive: no existing field moved or changed meaning.

### Changed

- **Period statistics release a finished week or month as soon as the date has
  moved past it.** `period_stats.build` accepted a `today` argument and never
  read it, so the last group in the curve was always treated as still running
  and kept out of best/worst. A complete August therefore stayed excluded until
  September's first snapshot landed — every 1st of the month before the
  collector ran, and every Monday morning for the week. The dashboard passes
  `today`; with it, a period whose calendar end is behind today counts as
  complete. Callers that omit `today` keep the old behaviour. This can change
  the "best month" / "worst week" records shown during that window, and only
  then.

### Upgrading

No migration. `docker compose pull && docker compose up -d`.

The rest is the first round of fixes from the Sonar way quality profile:
`build_payload_data`, the prompt registry and the CSV importer's `main` are
split into smaller functions, and the tests that wrapped several calls in one
`pytest.raises` block now isolate the call under test. **No figure changes from
any of that**: the dashboard payload built by the old and new `payload.py`
against the same live database was diffed field by field and is identical.

## [1.5.0] — 2026-09-07

The application image moves to Python 3.14, and CI now builds that image before
a change to it can be merged. No figure changes, no schema and no migration —
see **Upgrading**.

Minor rather than patch because the interpreter the image ships is part of what
it delivers, not an implementation detail: anything built `FROM` this image that
installs a wheel with no 3.14 build breaks across this boundary, and a patch
should not be able to do that. Nothing inside the application changed.

### Added

- **CI builds `app/Dockerfile`, then runs the suites inside the result.** Until
  this release nothing in CI ever built the image. `ci.yml` starts only the
  `postgres` service, the Python suites run on the host runner, and `make test`
  runs against the *pulled* image because `docker-compose.yml` declares
  `image:` and not `build:`. The publish workflow does build it — after merge.

  So a change to the Dockerfile could pass every check without anyone learning
  whether the image still built, and one did: a scanner's base-image PR
  swapping Debian for Alpine went green on all seven checks while
  `docker build` failed at the second of thirteen steps with
  `apt-get: not found`. The new job fails on that Dockerfile and passes on this
  one, which is the only evidence worth having that the gap is closed.

  Building alone is necessary and not sufficient, so the job also imports every
  pin and validates the crontab — a base image can build cleanly and still ship
  an interpreter your pins have no wheels for, which was true of that same PR.
  It builds amd64 only; the publish workflow covers arm64.

### Changed

- **The application image is built on `python:3.14-slim` instead of
  `python:3.13-slim`.** Verified in the built image: both suites pass, every
  pin imports, supercronic is present with a valid crontab, and `zoneinfo`
  still resolves. pandas 3.0.5 and streamlit 1.63.0 are the versions 1.4.0
  shipped; the only other difference is numpy 2.5.2 to 2.5.3, a patch-level
  float any rebuild would have picked up.

  This is a currency bump and **not** the security fix a scanner will keep
  proposing it as. Verified on 2026-09-07: `python:3.13-slim` and
  `python:3.14-slim` carried the identical `util-linux 2.41.5-0+deb13u1`, and
  `apt-cache policy` inside the image reported that same version as the newest
  available, from `trixie-security`. There was no patched util-linux to move
  to, so no Debian-based tag cleared `SNYK-DEBIAN13-UTILLINUX-17690419` — a
  medium-severity use-after-free on code paths this container never calls.
  Leaving Debian would clear it, at the cost of a port: this Dockerfile reaches
  for `apt-get` in three places and `useradd` in a fourth.

### Upgrading

No migration. `docker compose pull && docker compose up -d`.

**No figure changes.** No schema, no stored value and no computation was
touched. The engines, the dashboard and the MCP server are the same code as
1.4.0 — the only file under `app/` that differs is the Dockerfile, and the only
other change is the version number in `server.json` that the footer reports.
What moved is the interpreter underneath them.

**If you build your own image `FROM` this one, check it still builds before you
roll it out.** A wheel pinned to cp313, or a `pip install` of anything with no
3.14 build, will fail where it previously worked. This is the one case in this
release that can break something.

**Host installs (not compose) are unaffected.** They run whatever Python you
installed, and this release does not touch `app/requirements.txt`.

## [1.4.0] — 2026-09-03

A release about installing this on Windows. No figure changes, no schema and no
migration — see **Upgrading** — and nothing here alters an existing install on
macOS or Linux. What changes is that the documented path now works on all three
platforms, and one of the ways it previously did not was capable of losing a
backup.

The project told everyone to run `make`, which is a POSIX shell script runner
wearing a build tool's name. Its recipes reach for `sed`, `base64`, `gzip` and
`/dev/urandom`, so on Windows even a current `make.exe` cannot run six of the
thirty-one targets — including the bare `make` that lists them. The README's
`make`-free path was no better: it opened with `curl -fsSLO`, `$EDITOR` and
`chmod 600`, none of which do what they say on a stock Windows box.

### Added

- **`pdb.ps1`, a Windows runner** for the three targets that carry real logic:
  `init`, `backup` and `restore`. It runs on Windows PowerShell 5.1 — what a
  fresh Windows box actually has — as well as PowerShell 7. Everything else the
  Makefile does is a single `docker compose` command that is identical on every
  platform, so wrapping those would add a second thing to keep in step and buy
  nothing.

- **[docs/commands.md](docs/commands.md), every target beside the command it
  runs.** This is the reference for anyone without `make`, and CI now fails if a
  Makefile target exists that appears in neither this page nor `pdb.ps1` — the
  drift is otherwise silent and invisible to a Linux-only build.

- **A per-platform quick start**, with the Windows equivalents written out
  rather than left as an exercise: `curl.exe` (in PowerShell 5.1 the bare word
  `curl` is an alias for `Invoke-WebRequest`, which rejects those flags),
  `notepad`, and `icacls .env /inheritance:r` in place of `chmod 600` — on NTFS
  this is an ACL, and without dropping inherited entries the grant is merely
  additive and the file stays readable.
  - Also the first-run wall nobody warns you about: Windows refuses to run any
    local script by default, so `.\pdb.ps1` reports "running scripts is disabled
    on this system" until you allow it.

- **WSL2 named as the way to get the full Makefile on Windows**, since Docker
  Desktop's own backend is WSL2 and has almost certainly installed it already.
  With the warning that goes with it: keep the project in the Linux filesystem,
  and do not point the `./data` bind-mount override at a `/mnt/c` path.

### Fixed

- **A Windows backup could produce a corrupt archive and tell you it had
  worked.** This is the one item here that could have cost data. Translating
  `pg_dump | gzip > file` to Windows fails twice: there is no host `gzip`, and
  Windows PowerShell 5.1 decodes a native command's output as *text* before
  redirecting it, so the compressed stream is re-encoded on its way to disk.
  Measured on a real ledger, the same dump came out 5.6 MB and unreadable that
  way against 2.9 MB and valid. Nothing reports an error; `gzip -t` rejects the
  file, and otherwise you find out on the day you restore.
  - `pdb.ps1 backup` compresses **inside** the container and moves the finished
    file out with `docker compose cp`, so no binary data crosses the host shell.
    `docs/commands.md` documents that form for anyone doing it by hand, and CI
    asserts the round trip with `gzip -t` rather than checking that a file
    appeared. PowerShell 7 does not corrupt the redirect; the documented form
    works on both, so there is nothing to remember.
  - `pdb.ps1 restore` keeps the Makefile's guard and refuses to load a dump
    into a database that already has tables.

### Changed

- `CONTRIBUTING.md`, `CLAUDE.md`, `docs/operations.md`, `docs/csv-import.md`
  and `docs/exposure.md` now say where `make` does not apply and what to run
  instead. `docs/operations.md` gains a Windows Scheduled Task recipe beside the
  weekly cron entry.

### Upgrading

No migration. `docker compose pull && docker compose up -d`.

**No figure changes.** No schema, no stored value and no computation was
touched. Not one file under `app/` changed — the engines, the dashboard and the
MCP server are the same code as 1.3.0, and the only reason the image is rebuilt
at all is the version number in `server.json` that the footer reports. This
release is documentation, one new script, and tests.

**Nothing to do on macOS or Linux.** The Makefile is unchanged — not one recipe
was edited — so every `make` command keeps working exactly as before. `pdb.ps1`
is additive and irrelevant to those platforms.

**If you already installed on Windows**, the thing worth acting on is the backup
note above. Test any Windows-made dump you are relying on:

```powershell
docker compose cp .\backups\your-dump.sql.gz postgres:/tmp/t.gz
docker compose exec -T postgres sh -c 'gunzip -t /tmp/t.gz && echo VALID || echo CORRUPT'
docker compose exec -T postgres rm /tmp/t.gz
```

An untested backup is a hypothesis, and this is the release that says which
hypothesis was wrong.

## [1.3.0] — 2026-09-02

A security and hygiene release. No figure changes and no migration — see
**Upgrading** — but one shipped default is different, and if you reach the MCP
server from another machine you need to know which.

### Changed

- **The MCP server's ad-hoc runner now binds localhost by default** instead of
  all interfaces. `python -m app.mcp.server` is what someone types to try the
  server out, and a default that reaches the LAN is the wrong one for a process
  that answers questions about your ledger. Bearer auth sits in front either
  way — this narrows the blast radius of a misconfigured token, it does not
  close a hole.
  - **If you rely on reaching the MCP server from another machine, set
    `PORTFOLIODB_MCP_HOST=0.0.0.0`.** The shipped compose file already does,
    and passes `--host` to uvicorn itself rather than going through the runner,
    so containerised deployments are unaffected either way.

### Security

- **`python-dotenv` floor raised to 1.2.2** (CVE-2026-28684, arbitrary file
  overwrite via symlink following). The old `>=1.0` range permitted affected
  versions; a fresh install resolves to the newest match, so this closes the
  case where a resolver, a stale mirror, or an old lockfile lands on one that
  is vulnerable.
- The drift-checking tool refuses non-HTTP(S) URLs rather than handing whatever
  it is given to `urlopen`, which would otherwise read a local file and report
  on it as though it were the live site.
- **`.gitignore` now covers every `.env` sidecar, not just the known suffixes.**
  Copying your `.env` before editing it — `cp .env .env.bak` — is the obvious
  precaution, and under the old exact-match rules that copy was **untracked but
  not ignored**, one `git add -A` away from committing your credentials.
  `.env.template` stays visible.

### Fixed

- Nine `try`/`except`/`pass` blocks replaced with `contextlib.suppress`. All
  were best-effort cleanup — returning a connection, closing a handle, dropping
  a query parameter — and behave identically; the intent is now legible at a
  glance rather than inferred from an empty handler.

### Upgrading

No migration. `docker compose pull && docker compose up -d`.

**No figure changes.** No schema, no stored value and no computation was
touched.

**Read this if you reach the MCP server from another machine.** Two cases:

- **Running it through the shipped compose file — nothing to do.** Compose sets
  `PORTFOLIODB_MCP_HOST` and passes `--host` to uvicorn itself, so it never uses
  the changed default.
- **Running `python -m app.mcp.server` directly — set
  `PORTFOLIODB_MCP_HOST=0.0.0.0` in your `.env`**, or the server will answer
  only on localhost after this upgrade and remote clients will fail to connect.

Minor rather than patch because that default is a behaviour change someone can
be relying on, even though it is a default and overridable. The compose default
floats the major line, so a pull crosses this boundary on its own — which is
exactly why the case above is called out rather than left to be discovered.

Host installs (not compose) should re-run `pip install -r app/requirements.txt`
to pick up the `python-dotenv` floor.

## [1.2.2] — 2026-09-02

Three unused imports removed. **Nothing you can observe changes** — no figure,
no stored value, no schema, and no rendered output differs from 1.2.1.

### Fixed

- **Three imports that were never used are gone**: `datetime` from the
  positions service, and `json` and `fastmcp.prompts.Message` from the prompts
  module. Dead imports are not a runtime problem, but they are a standing
  question — a reader has to check whether the name matters before touching the
  file, and the answer here was no in all three cases.

### Upgrading

No migration, and no reason to hurry. `docker compose pull && docker compose
up -d` when convenient.

## [1.2.1] — 2026-09-02

Static-analysis hygiene. **Nothing you can observe changes** — no figure, no
stored value, no schema, and no rendered output differs from 1.2.0. It is tagged
rather than left on `main` so the clean analysis baseline belongs to a release
someone can pull.

### Changed

- **The dashboard front-end calls `Number.isNaN`, `Number.isFinite` and
  `Number.parseInt` instead of the bare globals.** The globals coerce their
  argument before testing it, which is why `isNaN("abc")` is `true` and
  `isNaN([])` is `false`. Neither trap could fire at the five call sites
  involved — every argument was already a number, and `Number.parseInt` *is*
  the global — so this is a consistency fix, not a bug fix. The same file had
  been using the `Number.*` form in three other places, and one convention is
  worth more than five exceptions to it.

- **The dashboard's news fetch carries the audit suppression its counterpart in
  the reporting path already had**, with a comment saying why the rule cannot be
  satisfied instead of leaving the next reader to work it out: the command's
  first element is the interpreter already running, and hardcoding a path there
  would break every virtualenv and container. The call itself is unchanged — no
  shell, and every other element a literal.

- Analysis config: the excluded-tests glob now matches the suites where they
  actually live, so a parameterized `DELETE` in a test helper stops being
  reported as a critical finding.

### Upgrading

No migration, and no reason to hurry. `docker compose pull && docker compose
up -d` when convenient.

## [1.2.0] — 2026-09-02

A release about the dashboard, and about one idea: a number you cannot trace is
a number you cannot use. Nothing here changes how a figure is computed or how
anything is stored — see **Upgrading**. What changes is how much of its own
working the dashboard is willing to show you, and whether it can be operated at
all without a mouse.

### Added

- **Every figure at the top of the Portfolio view opens the arithmetic behind
  it.** `Unrealized P&L +$841.94` was a claim with no route to its basis. Click
  or press Enter on any of the nine tiles and you get the definition in a
  sentence, the terms as an equation, and the rows they were summed from,
  ranked by contribution, with a button through to the fuller view.
  - Where the printed terms do not reconcile with the printed total, the panel
    says so rather than hiding it. The server rounds each term to the cent and
    the total from unrounded inputs, so a column can miss its own sum by a
    penny; on a panel whose entire purpose is showing the arithmetic, a sum that
    does not add up is worse than no sum, and quietly recomputing the total from
    the rounded terms would be worse still.
  - **Buying power** is the one figure derived from nothing — it is what you
    typed. Its panel names the account and the date you last entered it, and
    says plainly that nothing here contacts a broker.

- **The charts state what they are read against.** Four of them plotted a shape
  with no scale. The value chart and the price chart now carry value ticks and
  dates; the quarterly bars name their peak and the period they span, with each
  bar's own figure on hover. The drawer's chart — the one carrying your BUY and
  SELL markers — states its price extent and its date span, because a marker
  plotted against a scale you were never shown is decoration.
  - Ticks fall on the 1/2/5 ladder, which is why they can be labelled at all.
    The old gridlines sat at fixed fractions of the data range and landed on
    values like `8,912.47`.

- **A collection that was owed and did not happen is marked on the charts.** A
  straight line from Friday to Monday reads as a value moving smoothly through a
  weekend when in fact nothing was measured. Hatching every gap would be worse —
  a year would carry some fifty stripes for weekends alone — so the test is not
  how long a gap is but how much of the **collector window** it covers. Friday
  close to Monday open is 64 hours of wall clock and zero window minutes, and
  stays silent. Two hours missed on a Tuesday morning is 120, and does not.
  - Only from the first row in `snapshot_runs` onward. Run tracking began after
    collection did, so earlier history is sparse because nothing was running,
    not because anything was missed — the distinction between 86 marks on a
    year and the three that are real.
  - `market_window.open_minutes_between()` is the new arithmetic, next to the
    window definition the collector and the Settings page already share.

### Changed

- **The Portfolio view on a phone is ordered around the question you open it
  to ask.** It ran to 8,444px at 390×844 — ten full screens — with the list of
  what you own starting five screens down, behind a market strip, a returns
  strip and two charts. It is 3,792px now, the holdings begin at 1,466px, and
  the eight cards that are reference rather than answer keep their heading and
  open on request. Desktop is untouched; there is room for all of it at once
  there, which is the only reason the two differ.

- **Chart and category colour is now derived rather than chosen.** Every step of
  the palette is solved in OKLCH against **both** themes at once. The hand-picked
  hexes it replaces were only ever checked in one: six of nine fell below 3:1 on
  white and the Cash slice measured **1.48:1**, which is a slice you could not
  see in the default theme.
  - No category may borrow a hue that already means something. Every step stays
    clear of the gain, loss and interaction hues, so a sector can no longer
    render in loss-red.
  - The ramp is stored interleaved rather than sorted by hue, because slices
    consume it in order: sorted, the sector donut came out as one continuous
    sweep — a sequential scale pretending to be a set of names.

- **A news item's age reads in units a person holds in their head.** Hours ran
  to 48 with a decimal, so a two-day-old headline said `35.2h ago`. Past a day
  it is the day count.

- **Card headings are a size larger than the prose beneath them.** All twenty
  were 14px — the same size as body text, separated only by weight.

- **The topbar's right-hand group sits against the right edge.** Nothing in that
  row absorbed slack, so on a wide screen it packed left and left 452px of
  nothing after the last button.

### Fixed

- **The dashboard could not be operated from a keyboard.** Fifty-eight elements
  opened detail on click with no way to reach or trigger them otherwise, both
  overlays declared `aria-modal` without trapping focus, and one control moved
  focus out from under you on arrival.

- **Four ways the interface said something the data did not support.**
  - A partial collection was left in the value chart's domain, which both
    flattened the curve and **invented a maximum drawdown of about 49% that
    never happened**. Points the arithmetic cannot support are now excluded from
    the domain, the stroke and the drawdown scan alike, and the chart says how
    many it dropped.
  - Outside regular hours the price feed returns `0.0` rather than nothing, and
    that zero was printed as a bid and an ask of `$0.00`.
  - Rows whose price had gone stale were styled exactly like fresh ones.
  - A partial period was meant to be hatched and was not: the `background`
    shorthand later in the rule was overwriting the hatch.

- **"Top gainers" listed losers**, and "Top losers" listed gainers, whenever
  fewer than six symbols had moved that way — the list was taking the first six
  after sorting without checking the sign. It filters by sign now and says so
  when the list is short.

- **Lot counts implied a window that did not exist.** Headings read "7 most
  recent" and "97 most recent" where nothing had been truncated at all. They
  name the true count, and say `showing 12 of 23` only when a symbol genuinely
  exceeds the cap.

- **Which engine produced a cost basis is now stated where it is shown** —
  `FIFO` beside the figure — rather than left to be inferred. Both engines
  remain available and neither changed.

- **Contrast and layout failures across both themes and every supported width.**
  Text tones below 4.5:1, a dark-theme button at 3.74:1, initials measured
  against one stop of a gradient rather than across the blend, the heat map's
  unreadable band around zero, horizontal overflow at 320px on four views, and
  touch targets under 44px. The type scale went from nineteen sizes to nine and
  the corner scale from thirteen radii to seven.

- **Smaller things that read as faults.** A ticker whose name is its ticker
  stacked the same four letters twice (`VOOVOO`). "Mark all read" was live
  against nothing unread. The heat legend read `−3%+` at both ends. Stat tiles
  inside a card were themselves cards, drawing two borders around one boundary.
  The search field's `Ctrl K` hint lived inside the placeholder string and
  truncated mid-word to `Jump to anything… Ctr`.

- **The Data Health page failed by rendering an empty report.** A page whose
  whole subject is whether the data can be trusted was answering "nothing wrong
  here" when what had happened was that it could not look. It now says the
  report could not be built, that nothing has been checked, and offers a retry.

### Upgrading

No migration. `docker compose pull && docker compose up -d`.

**No figure changes.** No schema, no stored value, and no computation was
touched: the payload gained keys, none were removed or redefined, and the one
expression that was restructured — the change since the last snapshot — is the
same sum written as a difference of two totals over the same symbols, so that it
can show its own terms. Two displayed numbers do change, and both were wrong
before: a maximum drawdown that a partial snapshot had fabricated, and bid/ask
values of `$0.00` that were a feed placeholder rather than a price.

This is a **minor** bump under semver — it adds behaviour and takes nothing
away. The compose default floats the major line (`:1`), so `docker compose pull`
crosses this boundary on its own, which is exactly why the paragraph above says
what it says.

## [1.1.6] — 2026-09-01

### Added

- **The dashboard shows which release it is running.** Bottom of the sidebar,
  under your name. Until now the only way to answer that was to inspect the
  container, which is a poor answer to a question you ask precisely when
  something looks wrong.
  - The number is the release alone — `1.1.6`. Hovering it gives the exact
    build, which for a published image is the tag and the commit
    (`v1.1.6@<sha>`), and for a checkout is the commit on its own.
  - It reads `server.json`, the same file this project already bumps at every
    release, so there is no second version to keep in step. That file now ships
    inside the image, which it did not before.
  - A build that cannot identify itself says `unknown` rather than showing
    nothing. That is the honest answer and it is the case you most want to see.

### Changed

- **`app_version()` moved out of the MCP service tree into `app/version.py`.**
  It lived in `app/mcp/services/cutoff.py`, which imports a database pool and a
  package that shadows the official `mcp` SDK on the dashboard's import path —
  not something to pull in to render a footer line. Both the dashboard and the
  MCP provenance block now read from one small module. No behaviour changes:
  the MCP `app_version` field reports exactly what it did before.

### Upgrading

No migration. `docker compose pull && docker compose up -d`.

**1.1.5 was not re-issued.** It is published, and an image tagged `1.1.5`
keeps meaning the build that shipped as 1.1.5 — including for anyone who
already pulled it. Re-pointing that tag would have been especially wrong in
this release, whose whole subject is a footer that tells you which version you
are running: two different builds both answering `1.1.5` would break the
feature at the first question it is asked.

## [1.1.5] — 2026-09-01

### Fixed

- **`report_portfolio_db.py` failed to run for most of each day, in both
  `--mode daily` and `--mode eod`.** It chose its snapshot with `MAX(ts)` over
  `price_snapshots`, which since 1.1.0 also holds the Markets-strip benchmarks.
  Those index futures are collected around the clock, while the symbols you hold
  stop after the US close — so for most of the day the newest row in that table
  is a benchmark-only instant at which nothing in your portfolio has a price:

  ```
  09:30  n=3   ES=F, NQ=F, YM=F     <- what MAX(ts) selected
  23:13  n=13  the actual holdings
  ```

  With no symbol priced, every derived column became object dtype, pandas fell
  back to element-wise Python arithmetic, and dividing by the zero cost basis of
  a fully-closed position raised `ZeroDivisionError` instead of returning `inf`.
  Snapshot selection now excludes benchmarks, so the report asks the question it
  means: when were the *holdings* last priced.

- **On the runs where that report did work, it printed `inf%`.** The same
  division, with at least one symbol priced, stayed in numpy and produced
  infinity rather than raising — once for every fully-closed position, which is
  most of the symbol list on a mature ledger. A position with no cost basis now
  reports no percentage instead of an infinite one.
  - Only the displayed percentage was ever affected. Cost basis and realized
    P&L come from `lots` and never touch that column.

- **The daily briefing's price refresh only ever worked on one machine.** When
  the snapshot looked thin, the report shelled out to a hardcoded
  `powershell.exe` running a `run_snapshot.ps1` that is gitignored — so it does
  not exist in a clone, in the container, or anywhere but its author's box. The
  call sat inside a bare `except: pass`, so everywhere else the refresh failed
  silently and the briefing reported stale prices without saying so. It now runs
  the collector directly with the interpreter already in hand.
  - It still declines outside the configured collector window, deliberately.
    A briefing run at midnight should tell you the prices are old, not
    manufacture a snapshot.
  - A refresh that cannot run now says so in the output, and is bounded by a
    timeout so a stuck collector cannot hang the report.

### Upgrading

No migration. `docker compose pull && docker compose up -d`.

Nothing stored changes. If you use `report_portfolio_db.py`, it should now run
whenever you ask it to rather than only in the window after a price collection,
and the percentages next to closed positions will be blank where they used to
read `inf%`.

## [1.1.4] — 2026-08-31

### Fixed

- **CSV imports recorded snapshot timestamps at the wrong offset whenever the
  file named its timezone.** `import_csv_history.py` built its timezone lookup
  from a bare `pytz.timezone("America/New_York")`. A pytz zone object carries
  **LMT** — the earliest offset in the database, −4:56 for New York — until
  `.localize()` replaces it, and a value handed to a parser as a lookup never
  gets that call. So every row whose Time column carried an `EST` or `EDT` token
  was converted at −4:56:

  | Time column | correct UTC | recorded | error |
  | --- | --- | --- | --- |
  | `16:00 EST` | 21:00 | 20:56 | 4 minutes early |
  | `15:59 EDT` | 19:59 | 20:55 | 56 minutes late |

  - **Rows with no timezone token were always correct**, because that path did
    call `.localize()`. That is why this lasted: a spot-check against any export
    that omitted the token showed nothing wrong, and the two error sizes look
    like different bugs rather than one.
  - Both wrong values land on a `:56`/`:55` minute, so an EST row and an EDT row
    are indistinguishable by eye — but they are wrong in *opposite directions*
    and need opposite corrections. Anything that rounds the minute repairs one
    group and worsens the other.
  - The importer now uses `zoneinfo`. An explicit `EST`/`EDT` token maps to a
    fixed −5/−4, because the token states its own offset and re-deriving one from
    the date would silently move a row by an hour; a row with no token falls back
    to `ZoneInfo("America/New_York")`, which resolves the offset from the
    datetime itself.
  - **This fixes the importer, not rows already imported.** See **Upgrading**.

- **`create_ro_role.py --password` broke on a password containing a quote.** The
  read-only role's password was substituted into SQL *text* to fill the
  placeholder in `sql/create_ro_role.sql`, because the `CREATE ROLE` there sits
  inside a `DO` block, whose body is a string literal and cannot take a bound
  parameter. A quote in the password broke the statement; a crafted one could
  inject. The password is now bound to the `ALTER ROLE` that already followed,
  and the placeholder gets a throwaway. `--generate` was never affected — the
  generated value has no quote in it. `sql/create_ro_role.sql` is unchanged, so
  applying it by hand with `psql` works exactly as before.

- **`.env` lines with a one-character key were silently ignored.** All four
  loaders — the database credentials, the LLM keys, the MCP token, and the
  dashboard's — shared a regex that required a key of at least two characters,
  so `K=v` was skipped without a word. They now share one parser that does not.

### Changed

- **`fd_weekly_enrichment.py` reports failure instead of always exiting 0.** It
  returned success through a revoked API key, an unreachable database and a dead
  network alike, because its fetch helper turns every error into a payload rather
  than raising. It now exits non-zero when the run could not do what it was asked
  — persistence was requested and the database was unreachable, or every call
  that reached the vendor failed. Partial failures still exit 0 and are reported
  in the output, so a single flaky endpoint does not turn a weekly job red. **If
  you schedule this script, it can now fail a job that previously always
  passed** — that is the point, but it may be the first time you see it.

- **`import_csv_history.py --pattern` refuses to leave `--dir`.** `--dir` and
  `--pattern` were joined straight into a glob, so `--pattern '../../*.csv'` read
  files outside the directory named and imported them with nothing in the output
  saying where they came from. Absolute patterns and any `..` segment are now
  rejected before the glob runs, and surviving matches are re-checked against the
  resolved directory so a symlink pointing out of the tree is excluded too. A
  recursive `**/*.csv` inside `--dir` still works.

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

### Security

- **The image build now pins every download hop to HTTPS.** The supercronic
  fetch used `curl -fsSL`, which follows redirects, with nothing constraining the
  scheme a redirect could move to — a redirect to `http://` was followed happily.
  The SHA-256 check that follows would still have caught a swapped binary, but
  only after writing it to disk. `--proto '=https' --proto-redir '=https'
  --tlsv1.2` now applies to every hop.
- **The publish workflow no longer hands a registry-write token to the test
  job.** `packages: write` sat at workflow level, so the job that only runs the
  test suite — and every action it pulls — held a token that can push to GHCR.
  It is now granted to the publishing job alone. No effect on the published
  image; it narrows what a compromised action in CI could reach.

### Upgrading

No migration in this release. `docker compose pull && docker compose up -d`.

**If you have ever imported CSVs with `import_csv_history.py`, your existing
`price_snapshots` timestamps may be wrong** — the fix above corrects the importer
but does not touch rows already stored. Whether you are affected depends on
whether your CSVs named a timezone in the Time column.

To check, assuming your export's times fall on the hour:

```sql
SELECT to_char(ts AT TIME ZONE 'UTC', 'HH24:MI') AS utc_time, count(*)
FROM price_snapshots
WHERE source = 'csv'
GROUP BY 1 ORDER BY 2 DESC;
```

A `:56` or `:55` where you expect `:00` or `:59` is the LMT signature. Correcting
those rows means deciding, per row, which side of US daylight saving its date
falls on — rows imported between the March and November transitions are **56
minutes late**, the rest are **4 minutes early** — and then rewriting `ts`, which
is half of `price_snapshots`' primary key and a column this project otherwise
only ever appends to. Take a backup first. There is no automated repair in this
release, deliberately: the correction is not uniform and a wrong one is worse
than the current state, which is at least consistent.

For a market-close export the error stays inside the correct day, so daily and
longer-period figures are unaffected: cost basis and realized P&L come from
`lots` and never touch this column at all, and a shift of under an hour does not
change which snapshot is the day's latest. What moves is intra-day ordering and
anything compared against a market-close boundary. The exception worth knowing:
a 56-minute shift on a row already within an hour of midnight UTC lands on the
next date, so an export timestamped late in the evening ET can be attributed to
the wrong day.

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

[Unreleased]: https://github.com/amosgeva/PortfolioDB/compare/v1.7.2...HEAD
[1.7.2]: https://github.com/amosgeva/PortfolioDB/releases/tag/v1.7.2
[1.7.1]: https://github.com/amosgeva/PortfolioDB/releases/tag/v1.7.1
[1.7.0]: https://github.com/amosgeva/PortfolioDB/releases/tag/v1.7.0
[1.6.0]: https://github.com/amosgeva/PortfolioDB/releases/tag/v1.6.0
[1.5.0]: https://github.com/amosgeva/PortfolioDB/releases/tag/v1.5.0
[1.4.0]: https://github.com/amosgeva/PortfolioDB/releases/tag/v1.4.0
[1.3.0]: https://github.com/amosgeva/PortfolioDB/releases/tag/v1.3.0
[1.2.2]: https://github.com/amosgeva/PortfolioDB/releases/tag/v1.2.2
[1.2.1]: https://github.com/amosgeva/PortfolioDB/releases/tag/v1.2.1
[1.2.0]: https://github.com/amosgeva/PortfolioDB/releases/tag/v1.2.0
[1.1.6]: https://github.com/amosgeva/PortfolioDB/releases/tag/v1.1.6
[1.1.5]: https://github.com/amosgeva/PortfolioDB/releases/tag/v1.1.5
[1.1.4]: https://github.com/amosgeva/PortfolioDB/releases/tag/v1.1.4
[1.1.3]: https://github.com/amosgeva/PortfolioDB/releases/tag/v1.1.3
[1.1.2]: https://github.com/amosgeva/PortfolioDB/releases/tag/v1.1.2
[1.1.1]: https://github.com/amosgeva/PortfolioDB/releases/tag/v1.1.1
[1.1.0]: https://github.com/amosgeva/PortfolioDB/releases/tag/v1.1.0
[1.0.3]: https://github.com/amosgeva/PortfolioDB/releases/tag/v1.0.3
[1.0.2]: https://github.com/amosgeva/PortfolioDB/releases/tag/v1.0.2
[1.0.1]: https://github.com/amosgeva/PortfolioDB/releases/tag/v1.0.1
[1.0.0]: https://github.com/amosgeva/PortfolioDB/releases/tag/v1.0.0
