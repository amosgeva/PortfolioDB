# Importing history from CSV

`app/import_csv_history.py` bulk-loads trades **and** historical prices from CSV
files. It was written against Yahoo Finance portfolio exports, and any broker
export works once you rename a few columns.

There are no broker-specific adapters. This is on purpose: one documented column
set you map onto is easier to reason about than a dozen half-maintained parsers.

```bash
# Always dry-run first — it parses and reports without writing anything
docker compose run --rm dashboard python app/import_csv_history.py \
  --dir /app/import --default-account IBKR --dry-run

# Then for real
docker compose run --rm dashboard python app/import_csv_history.py \
  --dir /app/import --default-account IBKR
```

To make your files visible inside the container, mount them in your override:

```yaml
services:
  dashboard:
    volumes:
      - ./import:/app/import:ro
```

## Columns

One row per holding, as portfolio exports normally look. Nothing is
case-insensitive — header names must match exactly.

| Column | Required for | Meaning |
|---|---|---|
| `Symbol` | everything | Ticker. Uppercased on import. |
| `Trade Date` | lots | Trade date as `YYYYMMDD` (e.g. `20260213`). |
| `Purchase Price` | lots | Price per share, in the instrument's currency. For a sale, the price you sold at. |
| `Quantity` | lots | Shares. Always **positive** — see `Side`. |
| `Side` | lots (optional) | `BUY` or `SELL`. Absent means BUY. `B`/`BOT`/`BOUGHT` and `S`/`SLD`/`SOLD` are accepted. |
| `Commission` | lots (optional) | Fees for the trade; blank counts as 0. |
| `Comment` | lots (optional) | Free text. Also used for account mapping — see below. |
| `Date` | price snapshots | File's as-of date, e.g. `2026/02/20`. |
| `Time` | price snapshots | File's as-of time with a zone token, e.g. `16:00 EST`. |
| `Current Price` | price snapshots | Last price at that timestamp. |

Two independent things are imported:

- **A lot**, from any row that has `Trade Date` + `Purchase Price` + `Quantity`.
  Rows without all three are skipped for lots — which is what you want for a
  holding you never traded through this account.
- **A price snapshot** per row with a `Current Price`, stamped with the
  timestamp built from that file's `Date` + `Time`. **Each file is one instant**,
  so a directory of daily exports becomes a daily price series.

`Date`/`Time` are read as US-Eastern when the zone token is `EST`/`EDT`, and
converted to UTC before storage.

## Buys, sells, and the mistake worth avoiding

A **holdings export** — what most portfolio tools give you — is a list of things
you own, so every row is a purchase and you can leave `Side` out entirely.

A **trade history** contains sales, and it needs `Side`. If you import one
without it, every sale is recorded as another purchase: open quantity comes out
too high, realized P&L comes out as zero, and **nothing errors** — the numbers
are just wrong. The import summary now prints the split so you can check it
against your own expectation:

```
Lots inserted (attempted): 214 — 187 BUY, 27 SELL
```

If your export encodes sales as **negative quantities**, the row is rejected
rather than guessed at: a negative quantity is also how some brokers write a
short, an assignment or a correction, and picking wrong corrupts a cost basis in
a way that only surfaces months later. Flip the sign and add `Side=SELL`.

Sales are matched against your open lots by the FIFO engine at read time, so
they need no lot reference — just the date, quantity and price. A sale of more
than you hold is truncated with a warning, because shorts are not supported.

## Mapping any broker's export

There are no per-broker parsers, and the recipe is the same everywhere:

1. **Export the trade history**, not the current positions, if you want cost
   basis and realized P&L to be right. Positions alone give you neither.
2. **Open it in a spreadsheet and rename the headers** to the names in the table
   above. Delete every column you do not need — extras are ignored, so you can
   leave them, but a smaller file is easier to check.
3. **Reformat the trade date to `YYYYMMDD`.** This is the fiddly one. In
   spreadsheet terms: `=TEXT(A2,"yyyymmdd")`. The compact form is required
   precisely because `03/04/2026` means two different days on two continents.
4. **Make every quantity positive** and put the direction in `Side`.
5. **Split fees out** into `Commission` if the export bundles them into the
   price; per-share price and per-trade fees are separate here, because BUY fees
   inflate cost basis while SELL fees reduce proceeds.
6. **One file is one price instant.** If your file has a `Current Price` column,
   give the whole file one `Date` + `Time` — that is the timestamp its prices are
   stored under. Leave those three columns out and no prices are imported at all,
   which is usually right for a trade history: the collector fetches prices.
7. **`--dry-run` first**, then check `make positions` against your broker.

Currency is the one thing no mapping can fix: PortfolioDB is single-currency end
to end, so an export mixing currencies produces totals that are **wrong, not
approximate**. Import one currency, or one account per currency and read them
separately.

If you work out a clean mapping for a broker, an issue or a PR adding it here is
genuinely welcome — a recipe someone has actually run is worth more than one
inferred from a vendor's documentation.

## Accounts

The ledger is per-account, and CSV exports rarely carry an account column, so:

- `--default-account NAME` (required) is used for every lot, unless
- `--tagged-accounts A,B` is given, in which case a lot whose `Comment`
  mentions one of those names (case-insensitive substring) is assigned to it.

```bash
# lots whose Comment mentions "IBKR" land in IBKR; everything else in BrokerA
--default-account BrokerA --tagged-accounts IBKR
```

Accounts are free-form labels — call them whatever your statements call them.

## Duplicates

Safe to re-run. Lots collide on the unique index
`(symbol, account, trade_date, quantity, price)` and snapshots on the primary
key `(symbol, ts)`; both insert with `ON CONFLICT DO NOTHING`.

The consequence worth knowing: **two genuinely separate fills of the same size
at the same price on the same day in the same account import as one lot.** It's
the same trade-off the manual `add-lot` path has. Split the quantity or add a
distinguishing `Comment` if you need both rows.

## A sample file

[`examples/sample_portfolio.csv`](examples/sample_portfolio.csv) is a two-symbol
export in the expected shape — copy its header row and fill in your own data:

```csv
Symbol,Trade Date,Side,Purchase Price,Quantity,Commission,Comment,Date,Time,Current Price
AAPL,20260213,BUY,184.25,40,1.25,opening position,2026/05/20,16:00 EDT,195.10
AAPL,20260512,SELL,206.40,15,1.25,trimmed the winner,2026/05/20,16:00 EDT,195.10
VOO,20260220,BUY,505.10,12,0.00,IBKR core sleeve,2026/05/20,16:00 EDT,518.44
MSFT,,,,,,watchlist only - no position,2026/05/20,16:00 EDT,436.90
```

Four things that file demonstrates: two buys building a position, a partial sale
matched by FIFO on import-independent read, an account tag in the `Comment`, and
a row with prices but no trade — a watchlist symbol whose price history you want
without claiming you own it.

## Afterwards

```bash
make positions        # does the ledger look right?
```

Check a couple of positions against your broker before trusting the numbers —
cost basis is recomputed from the lots you just imported, so an import error
shows up as a wrong average cost rather than an error message. If something is
off, remember the ledger is append-only: fix it with a corrective entry (or
delete the specific lot row from the Manage page) rather than editing history in
place.

If your export has no `Trade Date` at all — common when a broker only gives you
current holdings — you have positions but no cost basis. Enter those few lots by
hand with `make add-lot` instead; guessing a purchase price silently corrupts
every P&L number downstream.
