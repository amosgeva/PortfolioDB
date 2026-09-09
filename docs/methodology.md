# Methodology

How PortfolioDB computes what it reports. Written for whoever reads a number and
needs to know what it actually means — including an agent deciding whether two
figures can be added together.

The short version of the thing most likely to be got wrong: **realized P&L is
already net of fees. Do not subtract fees from it again.**

---

## 1. The ledger

Four append-only tables. There is no positions table — open quantity, cost basis
and realized P&L are recomputed from `lots` on every read.

| Table | Holds |
| --- | --- |
| `instruments` | Symbol registry. `watchlist=TRUE` marks a non-held symbol that should still be priced. |
| `lots` | Every BUY/SELL. `side` plus a positive `quantity` encodes direction. |
| `price_snapshots` | Time-series quotes, PK `(symbol, ts)`. |
| `cash_snapshots` | Manual cash balances. Latest row per account wins. |
| `income` | Dividends, interest, distributions — deliberately outside `lots`. |
| `corporate_actions` | Splits, applied at read time (§6). |

Matching is scoped to `(symbol, account)` in both engines. Cross-account
fungibility happens only at the merge step in `portfolio.py`.

---

## 2. Fee treatment — read this before combining any two numbers

The engines fold fees into the trade:

```
BUY  cost per share      = (price × qty + fees) / qty      fees INFLATE cost
SELL proceeds per share  = (price × qty − fees) / qty      fees REDUCE proceeds
```

Therefore:

```
realized_pnl = (sell_proceeds_ps − buy_cost_ps) × qty      ← ALREADY NET OF FEES
```

Every endpoint that reports `realized_pnl`, `total_realized`, `unrealized_pnl`
or `total_return_pct` is reporting a figure net of fees.

`get_fees_summary` and the `fees` field of `get_trade_quality` are
**decompositions of that same money, not additional costs**. Adding them to a
realized figure double-counts. The invariant that makes this checkable:

```
gross_realized_pnl − fees == net_realized_pnl        (exact, asserted in tests)
```

`gross_realized_pnl` cannot be derived from the net figures alone, which is why
`fifo.MatchLine` carries `buy_fee_ps` and `sell_fee_ps` separately.

### Proportional allocation

A partial match takes its proportional share of the lot's fee. Matching 4 shares
of a 10-share BUY that cost \$5 in fees allocates \$2 — not \$5, not \$0.

### Fees on still-open positions are not realized

Only fees attached to closed parcels appear in realized figures. The remainder
sits inside the open cost basis. A worked example — a ledger that paid \$22.50
in buy fees and \$32.50 in sell fees and has since closed most of its parcels:

```
ledger total fees            55.00   (buy 22.50 + sell 32.50)
allocated to closed parcels  47.50
residual, in open cost basis  7.50   (≤ buy fees, by construction)
```

`get_fees` reports the same three figures for your own ledger.

### Avg-cost differs, deliberately

Under avg-cost the buy fee is folded into the running average and cannot be
recovered from it afterwards, so `gross_realized_pnl` is accumulated on a
*parallel* average carried at raw price. The identity above still holds, but the
allocated total differs slightly from FIFO (in the example above, \$48.00
against FIFO's \$47.50) because the two engines match different parcels. That is
a real difference between the methods, not a rounding artifact.

---

## 3. What counts as a trade

`get_trade_quality` defines a **trade** as one closing SELL lot.

FIFO can split one SELL across several BUY lots, and those parcels can land on
opposite sides of breakeven. Counting them separately would report a win rate
for events the user never experienced as separate decisions. Parcels are
reported as `match_count`.

Holding periods are bucketed **per parcel**, because a holding period is the gap
between one BUY and one SELL and only exists at that level. Under avg-cost
purchases are pooled, so there is no buy date and holding buckets report
`available: false` rather than a fabricated number.

| Metric | Definition |
| --- | --- |
| `win_rate_pct` | wins / (wins + losses) × 100. Breakeven trades are excluded from the denominator and counted separately. |
| `average_gain` | Mean net P&L of winning trades (positive). |
| `average_loss` | Mean net P&L of losing trades — **signed**, so negative, matching the sign convention everywhere else. |
| `payoff_ratio` | `average_gain / abs(average_loss)`. |
| `profit_factor` | Σ gains / abs(Σ losses). |
| `traded_notional` | Σ (quantity × price) over both sides, **excluding fees** — otherwise the fee would appear on both sides of the fee ratio. |
| `fee_to_gross_profit_pct` | Null when gross P&L is ≤ 0: divided by a loss the ratio inverts sign and reads as nonsense. |

`since`/`until` filter the **sell** side only. The BUY a sale matches may be far
older — this is how a tax report reads.

---

## 4. Returns

`get_period_returns` reports **time-weighted return**. Holdings are
reconstructed per snapshot day and daily sub-period returns are chained, so
money added is never counted as a gain and money taken out is never counted as
a loss.

```
r_i = (MV_i + div_i + out_i) / (MV_{i−1} + in_i) − 1
```

where `in_i` is what was paid for securities that day (fees included), `out_i`
what was received for securities sold (fees deducted), and `div_i` income
earned. See `app/twr.py`.

Trades carry a date, not a time, so a trade's own day is an approximation:
a purchase is valued from its price to that day's close, a sale from the
previous close to its price. A day on which nothing was held and nothing was
bought has no capital at work; the growth factor passes through it unchanged,
and a period made only of such days reports `null` rather than 0%. Income that
arrives after a position was sold is credited against the last capital that
earned it.

**Not available, and why:**

- **Money-weighted return / XIRR at portfolio level** — there is no
  external-flow ledger. `cash_snapshots` are manual balances only, so a deposit
  is indistinguishable from a market move.
- **Anything before the first price snapshot.** Coverage starts with your
  first collected quote, which on most ledgers is months after the first trade
  (the trades were entered from statements; the prices were not). Every
  "inception" figure is inception-of-coverage. The review snapshot reports
  `coverage_start` and the gap to your first trade, and `get_data_quality`
  names each holding whose first trade predates coverage.

The benchmark is a **price return** from `price_snapshots` and excludes the
benchmark's own dividends, making it slightly conservative against a
dividend-inclusive portfolio TWR. Reported as `dividends_included: false`.

---

## 5. Holdings basis for historical series

Series that value the past — `get_drawdown_stats`, `get_portfolio_value_history`
— accept `holdings_basis`:

- **`historical`** (default) reconstructs the holdings actually held at each
  point from the lot ledger.
- **`current_constant`** holds today's quantities across all of history. Kept
  for comparison only; it back-projects current positions onto a past that did
  not hold them, and on a real ledger it can report a drawdown several times
  the true one.

**Drawdown is measured on the time-weighted growth curve**, not on market
value. A market-value series falls when securities are sold and rises when
money is added, so a withdrawal read as a drawdown and a deposit could hide
one. `get_drawdown_stats` for the whole portfolio therefore runs on the same
daily flow-adjusted curve as §4 (`basis: "twr_growth_curve"`; `peak` and
`trough` are index levels with 1.0 at the first observation). A single symbol's
drawdown is on its split-adjusted price (`basis: "price"`); the
`current_constant` comparison keeps the old market-value definition
(`basis: "market_value_current_constant"`).

---

## 6. Splits

`corporate_actions` is the source of truth and is applied at **read** time —
`lots` and `price_snapshots` are never rewritten, preserving the append-only
invariant and keeping an action reversible by deleting its row.

`ratio` is new shares per old share (2:1 → `2`, 1:10 reverse → `0.1`). A record
before the ex-date is restated by multiplying quantity and dividing price, so
the money is unchanged and cost basis is unaffected.

Every surface reads the same restated ledger: the dashboard, the MCP tools,
the positions CLI, both reports and the dividend backfill all take their lots
and their price adjusters from one loader (`app/ledger_inputs.py`), so a
recorded split changes share counts, average cost, value history and returns
identically everywhere. Before 1.7.0 only the MCP tools applied the
adjustment; the dashboard and the reports showed pre-split share counts and a
split-sized drop in every return spanning the ex-date.

Two independent flags, because they answer different questions:

- `adjust_prices` — was the quote series rebased? Almost always true; this is
  what stops TWR reading a 2:1 split as −50%.
- `adjust_lots` — were extra shares actually credited to the account?

`ex_ts` (the ex-date's session open) is needed by raw-timestamp readers, which
would otherwise misclassify that day's pre-open snapshots.

Splits reported by the price vendor are recorded automatically by
`snapshot_prices.py`, with prices adjusted but **lots never** — the vendor knows
the quote series was rebased and cannot know whether the broker credited shares.
Those rows land `reviewed=FALSE` pending confirmation.

Beyond that, `app/check_splits.py` scans for unrecorded discontinuities
heuristically and **cross-references every hit against the vendor**. The
heuristic alone cannot distinguish a split from a one-day crash — both halve the
price overnight — and acting on it alone has produced a wrong adjustment once
already (see the PRIM correction in `docs/migration-notes.md`). Nothing is
adjusted automatically on heuristic evidence.

`kind='NONE'` records a discontinuity investigated and found *not* to be a
corporate action. Such rows carry `ratio = 1`, so they are inert by
construction, and they stop the scanner re-reporting a settled question.

---

## 7. One cutoff per request

`cutoff.resolve()` pins an instant and records which observation it landed on
per symbol and account. Lots filter on `trade_date <= cutoff.trade_date`, prices
and cash on `ts <= cutoff.ts`.

A timestamp alone is **not** sufficient. The collector stamps every row of a run
with the run's start time but commits them over 7–12 seconds, so a cutoff inside
that window reads a half-written run. `resolve()` therefore steps back behind any
run still in flight and reports the adjustment as
`as_of_adjusted_reason: snapshot_run_in_flight`.

Response-level provenance (`meta`) carries `as_of`, timezone, reporting currency,
cost-basis method, coverage window, and schema/app version. It is deliberately
response-level rather than per-field.

---

## 8. Data freshness

A symbol is **stale** when a snapshot run completed successfully but that symbol
got no price in it — judged against `snapshot_runs`, not the wall clock.

The measured weekend gap is **64.2 hours**, every weekend without variation
(Fri 23:13 → Mon 15:23 Jerusalem). Any hour-based threshold below that fires on
every held symbol every Monday; anything above it cannot detect a collector that
died mid-week. Comparing against the collector's own runs makes weekends,
holidays and DST structurally irrelevant.

Collector liveness is a separate portfolio-level check, alarming above 72h.

---

## 9. Nulls

A ratio whose denominator is absent returns `null` with an entry in
`null_reasons`, never `0.0` — those are different facts and a consumer cannot
tell them apart otherwise.

| Reason | Meaning |
| --- | --- |
| `no_cost_basis` | Nothing to return *on* (e.g. every position closed). |
| `no_prior_price` | No earlier valuation to move *from*. |
| `no_priced_positions` | Concentration undefined; `hhi: 0.0` would read as perfect diversification. |
| `no_winning_trades` / `no_losing_trades` | Payoff and profit factor need both sides. |
| `no_gross_profit` | Fee-to-profit ratio is meaningless against a loss. |
| `avg_cost_pools_purchases_so_no_buy_date` | Holding periods need a per-parcel buy date. |

Absolute totals still return `0.0` when genuinely zero. Only *derived ratios
with a missing denominator* are nullable.

---

## 10. Volume — recorded, not yet reported

`price_snapshots.volume` records the session's cumulative share count at the
moment of each snapshot. Collection began **2026-08-12**.

**Nothing computes on it, and nothing should for months.** An average-daily-volume
figure needs enough daily observations to mean anything, and there were none
before that date. The column exists so history starts accumulating, because that
cost only grows with waiting.

Two traps for whoever builds the first metric on it:

1. **It is cumulative within a session, not a daily total.** A 16:33 snapshot
   holds a fraction of the day's volume. Any daily figure must take the **last**
   snapshot per Asia/Jerusalem day — the same `DISTINCT ON ... ORDER BY ts DESC`
   pattern the daily price series uses — and should skip days whose last
   snapshot predates the close, since those undercount.
2. **NULL means "not reported", zero means "did not trade".** Thin instruments
   sometimes return nothing; that is recorded as NULL, never coerced to 0.

A related trap in the same table: `bid`/`ask` come back as `0.0` rather than NULL
outside regular hours, so a bid-ask spread built from these rows must treat a
zero quote as missing.

---

## 11. Reporting currency

`USD`, stated explicitly in every `meta` block. The ledger is single-currency:
every instrument is taken to be USD, there is no FX table and no rate source,
and nothing checks an instrument's quote currency. An instrument quoted in
another currency would be summed at face value — wrong, not approximate — so
import one currency, or one account per currency and read them separately
(see the CSV import guide). Endpoints accept
a `reporting_currency` input only to reject anything but USD rather than
pretending to convert.
