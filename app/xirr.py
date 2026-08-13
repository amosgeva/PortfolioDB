"""Money-weighted return (XIRR) — pure, DB-free.

The annualised rate that discounts a series of dated cash flows to zero. Unlike
TWR it *is* sensitive to the size and timing of the flows, which is the point:
TWR answers "how did the holdings perform", XIRR answers "what did I actually
earn on the money I put in, given when I put it in".

**Scope: investment-level only.** This is fed by trades, income and the closing
market value — not by deposits and withdrawals, which PortfolioDB does not
record. `cash_snapshots` holds manual balances with no flow ledger behind them,
so a deposit is indistinguishable from a market move and a portfolio-level
money-weighted return cannot be computed honestly. Anything reported from this
module must be labelled as the return on invested capital.

Sign convention, from the investor's point of view:

    BUY            negative  (money leaves you)
    SELL           positive
    dividend       positive
    closing value  positive  (what you would get back today)

Solved by bisection rather than Newton-Raphson: NPV is monotonic in the rate
over any range containing a single sign change, so bisection cannot diverge or
oscillate the way Newton does on the near-flat curves a short, lumpy flow series
produces. It costs a few dozen extra iterations and buys never returning a wrong
answer confidently.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable, Sequence

DAYS_PER_YEAR = 365.0

# Rates outside this bracket are not meaningful for a portfolio: -99.99% is
# near-total loss and +1000% a year is beyond anything this ledger will hold.
MIN_RATE = -0.9999
MAX_RATE = 10.0

# Bisection tolerances. 1e-10 on NPV is far below a cent on any realistic
# portfolio; 200 iterations is ~60 orders of magnitude of bracket reduction.
NPV_TOLERANCE = 1e-10
MAX_ITERATIONS = 200


@dataclass(frozen=True)
class CashFlow:
    when: date
    amount: float
    kind: str = ""  # 'buy' | 'sell' | 'income' | 'closing_value', for tracing


def npv(rate: float, flows: Sequence[CashFlow], base: date) -> float:
    """Net present value of the flows at ``rate``, discounted from ``base``."""
    total = 0.0
    for f in flows:
        years = (f.when - base).days / DAYS_PER_YEAR
        # (1+rate) can reach 0 only at rate = -1, excluded by MIN_RATE.
        total += f.amount / ((1.0 + rate) ** years)
    return total


def compute(flows: Iterable[CashFlow]) -> dict:
    """Solve for the annualised money-weighted rate.

    Returns {rate, rate_pct, status, ...}. Never raises on unsolvable input —
    a portfolio with no closing position, or one whose flows all point the same
    way, has no rate rather than a rate of zero, and says which.
    """
    flows = sorted(flows, key=lambda f: f.when)

    if len(flows) < 2:
        return _unsolved("insufficient_flows", flows)

    positives = [f for f in flows if f.amount > 0]
    negatives = [f for f in flows if f.amount < 0]
    if not positives or not negatives:
        # Without a sign change NPV never crosses zero: money only ever went in,
        # or only ever came out.
        return _unsolved("no_sign_change", flows)

    base = flows[0].when
    if flows[-1].when == base:
        return _unsolved("zero_duration", flows)

    lo, hi = MIN_RATE, MAX_RATE
    npv_lo, npv_hi = npv(lo, flows, base), npv(hi, flows, base)
    if npv_lo * npv_hi > 0:
        # Both ends the same sign — the root lies outside anything meaningful.
        return _unsolved("rate_outside_bracket", flows)

    for _ in range(MAX_ITERATIONS):
        mid = (lo + hi) / 2.0
        value = npv(mid, flows, base)
        if abs(value) < NPV_TOLERANCE or (hi - lo) < 1e-12:
            return _solved(mid, flows, base)
        if value * npv_lo < 0:
            hi = mid
        else:
            lo, npv_lo = mid, value

    return _solved((lo + hi) / 2.0, flows, base)


def _solved(rate: float, flows: Sequence[CashFlow], base: date) -> dict:
    return {
        "rate": rate,
        "rate_pct": rate * 100.0,
        "status": "ok",
        "null_reason": None,
        "flow_count": len(flows),
        "first_flow": base.isoformat(),
        "last_flow": flows[-1].when.isoformat(),
        "years": (flows[-1].when - base).days / DAYS_PER_YEAR,
        "basis": "money_weighted_return_xirr",
        "scope": "invested_capital_only",
    }


def _unsolved(reason: str, flows: Sequence[CashFlow]) -> dict:
    return {
        "rate": None,
        "rate_pct": None,
        "status": "unavailable",
        "null_reason": reason,
        "flow_count": len(flows),
        "first_flow": flows[0].when.isoformat() if flows else None,
        "last_flow": flows[-1].when.isoformat() if flows else None,
        "years": None,
        "basis": "money_weighted_return_xirr",
        "scope": "invested_capital_only",
    }


def from_ledger(
    lots: Iterable[dict],
    income: Iterable[dict],
    closing_value: float,
    closing_date: date,
) -> dict:
    """Build the flow series from ledger rows and solve.

    Args:
        lots: {side, trade_date, quantity, price, fees}. BUY costs include fees;
            SELL proceeds are net of them, matching the engines.
        income: {pay_date, amount}.
        closing_value: market value of what is still held at ``closing_date``.
            Included as a final positive flow — the liquidation you would
            receive — which is what makes an open position contribute.
    """
    flows: list[CashFlow] = []
    for lot in lots:
        gross = float(lot["quantity"]) * float(lot["price"])
        fees = float(lot.get("fees") or 0.0)
        if str(lot["side"]).upper() == "BUY":
            flows.append(CashFlow(lot["trade_date"], -(gross + fees), "buy"))
        else:
            flows.append(CashFlow(lot["trade_date"], gross - fees, "sell"))

    for row in income:
        flows.append(CashFlow(row["pay_date"], float(row["amount"]), "income"))

    if closing_value:
        flows.append(CashFlow(closing_date, float(closing_value), "closing_value"))

    return compute(flows)
