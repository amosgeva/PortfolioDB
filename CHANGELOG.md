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

[Unreleased]: https://github.com/amosgeva/PortfolioDB/compare/v1.2.0...HEAD
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
