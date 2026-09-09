"""One prepared view of the ledger, for every reader.

The engines in ``fifo`` / ``avg_cost`` and the aggregation in ``portfolio`` are
shared by every surface — dashboard, MCP, positions CLI, both reports — and for
a long time that was described as the parity guarantee. It was not one. A
shared algorithm over *differently prepared inputs* gives different answers,
and the preparation that matters is the split adjustment: the MCP services
loaded ``corporate_actions`` and restated lots and prices before running the
engines, while the dashboard, the CLI and the reports fed the engines raw
rows. Ten shares bought at 100, a recorded 2:1 split and a 50 quote read as
twenty shares worth 1,000 with a 0% return on one surface and as ten shares
with a −50% day on the others (audit F03, 2026-09-09).

So the parity contract moves one level up. This module is the single place
that turns database rows into engine inputs:

* ``load(conn, …)`` reads the lots (in FIFO processing order, optionally
  filtered) and every recorded corporate action, and returns a
  ``PreparedLedger`` whose ``lots`` are already restated into post-split units.
* ``prepare(lot_rows, actions)`` is the pure half, for callers that already
  hold rows (the MCP positions service composes its own filtered query) and
  for tests.
* The ``PreparedLedger`` carries the actions, so the same reader can restate
  its price series with ``price_by_day`` / ``price_points`` / ``price_rows``
  and both sides of every valuation are in the same units. Applying the
  factor to one side and not the other is the one way to make it worse than
  raw, which is why the adjusters live on the object that did the lots.

Each factor is applied exactly once. Nothing here caches: the dashboard and
the MCP positions service keep their own short-lived memos of the *results*
(see CLAUDE.md on why those TTLs must stay short), and the preparation itself
is cheap next to the queries it follows.

Still raw by design: the Manage page's trade-history table and the CSV
importer, which show and write what the operator actually entered.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable, Mapping

from psycopg2 import sql

import corporate_actions
from corporate_actions import CorporateAction
from db import fetch_all

# The one lot query, in the column order and processing order the engines
# expect. Every reader that wants "the lots" goes through load(); a reader that
# needs a different filter composes on top of this with psycopg2.sql.
LOTS_SQL = sql.SQL(
    "SELECT id, symbol, account, side, trade_date, quantity, price, fees "
    "FROM lots WHERE 1=1"
)
LOTS_ORDER = sql.SQL(" ORDER BY symbol, COALESCE(account,''), trade_date, id")


@dataclass(frozen=True)
class PreparedLedger:
    """Lots restated into post-split units, plus the actions that did it."""

    lots: list[dict[str, Any]]
    actions: tuple[CorporateAction, ...]

    @property
    def adjusted_symbols(self) -> frozenset[str]:
        """Symbols with at least one action that changes something."""
        return frozenset(
            a.symbol.upper() for a in self.actions
            if a.ratio != 1 and (a.adjust_lots or a.adjust_prices)
        )

    def price_by_day(
        self, price_by_day: Mapping[date, Mapping[str, float]]
    ) -> dict[date, dict[str, float]]:
        """Restate a {day: {symbol: price}} map — the TWR / returns shape."""
        return corporate_actions.adjust_price_by_day(price_by_day, self.actions)

    def price_points(
        self, points: Iterable[tuple[Any, str, float]]
    ) -> list[tuple[Any, str, float]]:
        """Restate (when, symbol, price) triples; ``when`` is a date or a ts."""
        return corporate_actions.adjust_price_points(points, self.actions)

    def price_rows(
        self,
        rows: Iterable[Mapping[str, Any]],
        *,
        ts_key: str = "ts",
        symbol_key: str = "symbol",
        price_key: str = "last_price",
    ) -> list[dict[str, Any]]:
        """Restate query rows of the {ts, symbol, last_price} shape.

        Returns fresh dicts. A row with no price, or no timestamp to judge the
        boundary by, passes through unchanged rather than guessing.
        """
        out: list[dict[str, Any]] = []
        if not self.adjusted_symbols:
            return [dict(r) for r in rows]
        for r in rows:
            row = dict(r)
            price = row.get(price_key)
            when = row.get(ts_key)
            sym = row.get(symbol_key)
            if price is None or when is None or sym is None:
                out.append(row)
                continue
            if str(sym).upper() in self.adjusted_symbols:
                (_, _, adjusted), = self.price_points([(when, str(sym), float(price))])
                row[price_key] = adjusted
            out.append(row)
        return out


def prepare(
    lot_rows: Iterable[Mapping[str, Any]], actions: Iterable[CorporateAction]
) -> PreparedLedger:
    """Pure half of load(): restate the given rows with the given actions."""
    acts = tuple(actions)
    lots = corporate_actions.adjust_lot_rows([dict(r) for r in lot_rows], acts)
    return PreparedLedger(lots=lots, actions=acts)


def load(
    conn,
    *,
    symbol: str | None = None,
    account: str | None = None,
    as_of: date | None = None,
) -> PreparedLedger:
    """Read lots and corporate actions from an open connection and prepare them.

    Filters are optional and bound as parameters. ``as_of`` keeps lots with
    ``trade_date <= as_of`` — the same rule the MCP cutoff uses. The corporate
    actions are always loaded in full: an action after ``as_of`` must not be
    applied, and ``lot_factor`` already skips it by comparing ex-dates, so
    filtering the actions here would only create a second rule to keep in sync.
    """
    query = LOTS_SQL
    params: list[Any] = []
    if symbol is not None:
        query += sql.SQL(" AND symbol = %s")
        params.append(symbol.upper())
    if account is not None:
        query += sql.SQL(" AND account = %s")
        params.append(account)
    if as_of is not None:
        query += sql.SQL(" AND trade_date <= %s")
        params.append(as_of)
    query += LOTS_ORDER

    rows = fetch_all(conn, query, tuple(params))
    actions = corporate_actions.fetch_actions(conn)
    if as_of is not None:
        # An action dated after the requested day has not happened yet from
        # that day's point of view; restating into units that do not exist
        # yet would misstate the position as of that day.
        actions = [a for a in actions if a.ex_date <= as_of]
    return prepare(rows, actions)
