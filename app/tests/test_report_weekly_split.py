"""The whole weekly report, run over a week that contains a split.

1.7.2 re-audit, F03 case A: `positions_as_of()` restated share counts into
each date's units while `price_map()` returned raw quotes, and the trade
branches consumed raw rows, so a pure 2:1 split inside the week was printed as
a 50% loss with a `qty Δ +10`. These tests run the real `main()` — the helper
tests that existed did not reach the arithmetic that was wrong — with the
database replaced at `fetch_all` / `ledger_inputs.load` and the clock fixed.
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ledger_inputs  # noqa: E402
import report_weekly_db as weekly  # noqa: E402
from corporate_actions import CorporateAction  # noqa: E402

MON, TUE, WED, THU, FRI, SAT = (date(2026, 3, 2) + timedelta(days=i) for i in range(6))
BEFORE = date(2026, 2, 20)
NOW = datetime.combine(SAT, time(10, 0)).replace(tzinfo=weekly.IL_TZ)
START_TS = datetime.combine(MON, time(23, 0)).replace(tzinfo=weekly.IL_TZ)
END_TS = datetime.combine(FRI, time(23, 0)).replace(tzinfo=weekly.IL_TZ)


def _lot(i, symbol, side, qty, price, day, account="IBKR"):
    return {"id": i, "symbol": symbol, "account": account, "side": side, "trade_date": day,
            "quantity": Decimal(qty), "price": Decimal(price), "fees": Decimal("0"), "notes": None}


# AAA splits 2:1 on Wednesday; CCC reverse-splits 1:4 the same day; BBB is an
# ordinary holding that gains 20% so the contributor block has a real entry.
ACTIONS = [
    CorporateAction(symbol="AAA", kind="SPLIT", ex_date=WED, ratio=Decimal("2")),
    CorporateAction(symbol="CCC", kind="REVERSE_SPLIT", ex_date=WED, ratio=Decimal("0.25")),
]
HELD_BEFORE = [
    _lot(1, "AAA", "BUY", "10", "100", BEFORE),
    _lot(2, "BBB", "BUY", "5", "10", BEFORE),
    _lot(3, "CCC", "BUY", "8", "10", BEFORE),
]
# Entered in the units of their day: the Tuesday sale is 2 pre-split shares at
# $104 (→ 4 at $52), the Thursday buy is 2 post-split shares at $52.
WEEK_TRADES = [
    _lot(4, "AAA", "SELL", "2", "104", TUE),
    _lot(5, "AAA", "BUY", "2", "52", THU),
]
# Raw quotes as the collector stored them: AAA 100 → 50 and CCC 10 → 40 are the
# actions, BBB 10 → 12 is a move.
PRICES = {
    START_TS: {"AAA": Decimal("100"), "BBB": Decimal("10"), "CCC": Decimal("10")},
    END_TS: {"AAA": Decimal("50"), "BBB": Decimal("12"), "CCC": Decimal("40")},
}


class _FixedClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


class _Conn:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _run(monkeypatch, capsys, lots, cash=(), prices=None):
    """Run main() over ``lots`` (raw rows), ``cash`` ({account, cash, ts} rows)
    and ``prices`` ({ts: {symbol: raw quote}}, default PRICES). The snapshot
    boundary queries honour their `symbol = ANY(%s)` filter like Postgres."""
    prices = prices or PRICES

    def fake_load(conn, *, symbol=None, account=None, as_of=None, units="as_of"):
        rows = [r for r in lots if as_of is None or r["trade_date"] <= as_of]
        return ledger_inputs.prepare(rows, ACTIONS, as_of=None if units == "current" else as_of)

    def _boundary(sql_one, params):
        syms = None
        if "symbol = ANY(%s)" in sql_one:
            syms = set(params[-1])
        cands = [ts for ts, quotes in prices.items() if syms is None or set(quotes) & syms]
        if "ts >= %s" in sql_one:
            cands = [ts for ts in cands if ts >= params[0]]
        return [{"ts": (min if "MIN(ts)" in sql_one else max)(cands) if cands else None}]

    def fake_fetch_all(conn, sql, params=None):
        sql_one = " ".join(sql.split())
        if "MIN(ts)" in sql_one or "MAX(ts)" in sql_one:
            return _boundary(sql_one, params)
        if "FROM price_snapshots WHERE ts=%s" in sql_one:
            return [{"symbol": s, "last_price": p} for s, p in prices[params[0]].items()]
        if "FROM cash_snapshots" in sql_one:
            latest: dict = {}
            for r in sorted(cash, key=lambda r: r["ts"]):
                if r["ts"] <= params[0]:
                    latest[r["account"]] = r
            return list(latest.values())
        if "FROM lots" in sql_one:
            lo, hi = params
            return [dict(r) for r in lots if lo <= r["trade_date"] <= hi]
        raise AssertionError(f"unexpected query: {sql_one}")

    no_fd = SimpleNamespace(
        ETF_SYMBOLS=set(),
        latest_facts=lambda conn, sym: None, latest_metrics=lambda conn, sym: None,
        recent_financials=lambda conn, sym, kind, limit=1: [],
        recent_earnings=lambda conn, sym, limit=1: [], recent_filings=lambda conn, sym, limit=20: [],
        recent_news=lambda conn, sym, limit=10: [],
    )
    monkeypatch.setattr(weekly, "datetime", _FixedClock)
    monkeypatch.setattr(weekly, "load_config", lambda: {})
    monkeypatch.setattr(weekly, "connect", lambda cfg: _Conn())
    monkeypatch.setattr(weekly, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(weekly.ledger_inputs, "load", fake_load)
    monkeypatch.setattr(weekly, "fd_store", no_fd)
    weekly.main()
    return capsys.readouterr().out


def _section(out: str, header: str) -> list[str]:
    """The lines of one report block: from its header to the next blank line."""
    lines = out.splitlines()
    start = next(i for i, ln in enumerate(lines) if header in ln) + 1
    block = []
    for ln in lines[start:]:
        if not ln.strip():
            break
        block.append(ln)
    return block


def _line(out: str, prefix: str, header: str = "TOP CONTRIBUTORS") -> str:
    matches = [ln for ln in _section(out, header) if ln.startswith(prefix)]
    assert len(matches) == 1, (header, prefix, matches)
    return matches[0]


def test_a_pure_split_contributes_nothing_and_changes_no_value(monkeypatch, capsys):
    out = _run(monkeypatch, capsys, HELD_BEFORE)
    # Start and end are both $1,000 (AAA) + $50 → $60 (BBB) + $80 (CCC): only BBB moved.
    assert "Weekly change:    $10.00" in out
    assert "Start securities: $1,130.00" in out and "End securities:   $1,140.00" in out
    aaa = _line(out, "AAA:")
    assert aaa.startswith("AAA: $0.00 (+0.00%) | $1,000.00 → $1,000.00")
    assert "qty Δ" not in aaa, "the extra shares are the split, not a purchase"
    ccc = _line(out, "CCC:")
    assert ccc.startswith("CCC: $0.00 (+0.00%) | $80.00 → $80.00") and "qty Δ" not in ccc
    assert _line(out, "BBB:").startswith("BBB: $10.00 (+20.00%) | $50.00 → $60.00")
    # The action is reported as what it is, in its own block.
    assert "🔀 CORPORATE ACTIONS THIS WEEK" in out
    assert "AAA: SPLIT ×2 on 2026-03-04" in out and "CCC: REVERSE_SPLIT ×0.25 on 2026-03-04" in out


def test_trades_on_both_sides_of_the_ex_date_are_measured_in_one_unit_basis(monkeypatch, capsys):
    out = _run(monkeypatch, capsys, HELD_BEFORE + WEEK_TRADES)
    # Held 20 (post-split) at a flat $50: no price move. Sold 4 at $52 against
    # the $50 start quote: +$8. Bought 2 at $52, worth $50 at the end: −$4.
    aaa = _line(out, "AAA:")
    assert aaa.startswith("AAA: $4.00 (+0.40%) | $1,000.00 → $900.00 | trade P&L $4.00 | qty Δ -2.0000")
    # The listing shows what was entered, in the units of the day.
    assert "2026-03-03 IBKR SELL AAA 2.0000 @ $104.00" in out
    assert "2026-03-05 IBKR BUY AAA 2.0000 @ $52.00" in out


def test_the_report_runs_to_its_last_section(monkeypatch, capsys):
    out = _run(monkeypatch, capsys, HELD_BEFORE + WEEK_TRADES)
    assert "⚠️ No FD data on file for: AAA, BBB, CCC" in out.replace("\n", " ") or "No FD data" in out


def test_a_week_without_actions_prints_no_action_block(monkeypatch, capsys):
    monkeypatch.setattr(weekly.ledger_inputs, "prepare", ledger_inputs.prepare)
    lots = [_lot(1, "BBB", "BUY", "5", "10", BEFORE)]
    out = _run(monkeypatch, capsys, lots)
    assert "CORPORATE ACTIONS" not in out
    assert _line(out, "BBB:").startswith("BBB: $10.00 (+20.00%)")


@pytest.mark.parametrize("ts,expected", [(START_TS, Decimal("50")), (END_TS, Decimal("50"))])
def test_price_map_restates_through_the_ledger(monkeypatch, ts, expected):
    monkeypatch.setattr(weekly, "fetch_all",
                        lambda conn, sql, params=None: [{"symbol": "AAA", "last_price": PRICES[params[0]]["AAA"]}])
    ledger = ledger_inputs.prepare(HELD_BEFORE, ACTIONS, as_of=FRI)
    assert weekly.price_map(object(), ts, ledger) == {"AAA": expected}
    # Without a ledger the raw quote comes back, as before.
    assert weekly.price_map(object(), ts) == {"AAA": PRICES[ts]["AAA"]}


# ── contribution arithmetic (1.7.3 re-audit, N05) ─────────────────────────────
#
# One symbol, DDD, no corporate action, a real 20% move: the flat-price split
# fixtures above cannot tell a right sale reference price from a wrong one.

import re  # noqa: E402

MOVE = {START_TS: {"DDD": Decimal("100")}, END_TS: {"DDD": Decimal("120")}}
_MONEY = r"\$(-?[\d,]+\.\d\d)"


def _money(s: str) -> Decimal:
    return Decimal(s.replace(",", ""))


def _weekly_change(out: str) -> Decimal:
    return _money(re.search(r"Weekly change:\s+" + _MONEY, out).group(1))


def _contributions(out: str) -> dict[str, Decimal]:
    return {m.group(1): _money(m.group(2))
            for ln in _section(out, "TOP CONTRIBUTORS")
            for m in [re.match(r"^(\w+): " + _MONEY, ln)] if m}


def _cash(*points):
    """Cash rows for one account, ``(ts, amount)`` points."""
    return [{"account": "IBKR", "cash": Decimal(a), "ts": ts} for ts, a in points]


TUE_TS, THU_TS = (datetime.combine(d, time(18, 0)).replace(tzinfo=weekly.IL_TZ) for d in (TUE, THU))


class TestContributionIdentity:
    """Σ contributions == Δ(securities + cash) whenever cash mirrors the trades."""

    def _check(self, monkeypatch, capsys, lots, cash, prices=MOVE):
        out = _run(monkeypatch, capsys, lots, cash=cash, prices=prices)
        contribs = _contributions(out)
        assert sum(contribs.values(), Decimal("0")) == _weekly_change(out), out
        return out, contribs

    def test_partial_sale_counts_the_sold_shares_move_once(self, monkeypatch, capsys):
        lots = [_lot(1, "DDD", "BUY", "10", "100", BEFORE), _lot(2, "DDD", "SELL", "5", "110", TUE)]
        out, c = self._check(monkeypatch, capsys, lots, _cash((START_TS - timedelta(days=1), "0"), (TUE_TS, "550")))
        assert c["DDD"] == Decimal("150")                      # 5 × 20 kept + 5 × 10 sold; not 250
        assert _line(out, "DDD:").startswith("DDD: $150.00 (+15.00%) | $1,000.00 → $600.00 | trade P&L $-50.00 | qty Δ -5.0000")

    def test_full_liquidation(self, monkeypatch, capsys):
        lots = [_lot(1, "DDD", "BUY", "10", "100", BEFORE), _lot(2, "DDD", "SELL", "10", "110", TUE)]
        _, c = self._check(monkeypatch, capsys, lots, _cash((START_TS - timedelta(days=1), "0"), (TUE_TS, "1100")))
        assert c["DDD"] == Decimal("100")                      # not 300

    def test_round_trip_from_cash(self, monkeypatch, capsys):
        lots = [_lot(1, "DDD", "BUY", "5", "105", TUE), _lot(2, "DDD", "SELL", "5", "110", THU)]
        out, c = self._check(monkeypatch, capsys, lots,
                             _cash((START_TS - timedelta(days=1), "1000"), (TUE_TS, "475"), (THU_TS, "1025")))
        assert c["DDD"] == Decimal("25")                       # 5 × (120 − 105) + 5 × (110 − 120); not 125
        assert "qty Δ" not in _line(out, "DDD:")

    def test_a_trade_dated_on_the_baseline_day_is_part_of_the_opening_position(self, monkeypatch, capsys):
        lots = [_lot(1, "DDD", "BUY", "10", "90", MON)]
        out, c = self._check(monkeypatch, capsys, lots, _cash((START_TS - timedelta(days=1), "100")))
        assert c["DDD"] == Decimal("200")                      # 10 × (120 − 100) between the snapshots; not 500
        assert "trade P&L" not in _line(out, "DDD:")
        assert "contributions count trades after 2026-03-02" in out
        assert "2026-03-02 IBKR BUY DDD 10.0000 @ $90.00" in out, "the listing still shows the calendar week"

    def test_fees_are_counted_once(self, monkeypatch, capsys):
        lots = [_lot(1, "DDD", "BUY", "10", "100", BEFORE),
                dict(_lot(2, "DDD", "SELL", "5", "110", TUE), fees=Decimal("2"))]
        _, c = self._check(monkeypatch, capsys, lots, _cash((START_TS - timedelta(days=1), "0"), (TUE_TS, "548")))
        assert c["DDD"] == Decimal("148")

    def test_falling_prices(self, monkeypatch, capsys):
        down = {START_TS: {"DDD": Decimal("100")}, END_TS: {"DDD": Decimal("80")}}
        lots = [_lot(1, "DDD", "BUY", "10", "100", BEFORE), _lot(2, "DDD", "SELL", "5", "90", TUE)]
        _, c = self._check(monkeypatch, capsys, lots, _cash((START_TS - timedelta(days=1), "0"), (TUE_TS, "450")), prices=down)
        assert c["DDD"] == Decimal("-150")                     # 5 × −20 kept + 5 × −10 sold

    def test_trades_around_a_split_with_a_real_move(self, monkeypatch, capsys):
        """AAA 2:1 on Wednesday and +20% in adjusted terms (raw 100 → 60);
        CCC 1:4 the same day and +10% (raw 10 → 44). Same trades as the
        flat-price case: sell 2 pre-split at $104, buy 2 post-split at $52."""
        prices = {START_TS: {"AAA": Decimal("100"), "CCC": Decimal("10")},
                  END_TS: {"AAA": Decimal("60"), "CCC": Decimal("44")}}
        lots = [_lot(1, "AAA", "BUY", "10", "100", BEFORE), _lot(3, "CCC", "BUY", "8", "10", BEFORE)] + WEEK_TRADES
        _, c = self._check(monkeypatch, capsys, lots,
                           _cash((START_TS - timedelta(days=1), "0"), (TUE_TS, "208"), (THU_TS, "104")), prices=prices)
        # 20 × (60 − 50) + 4 × (52 − 60) + 2 × (60 − 52) = 200 − 32 + 16
        assert c["AAA"] == Decimal("184")
        assert c["CCC"] == Decimal("8")                        # 2 × (44 − 40)


class TestWeekBoundaries:
    """Found in round 3: the market-overview collector writes futures rows at
    other times of day, and the first snapshot at or after Monday 16:15 was
    often one of those — quoting nothing the ledger holds."""

    def test_futures_only_snapshots_are_not_week_boundaries(self, monkeypatch, capsys):
        mon_futures = datetime.combine(MON, time(18, 0)).replace(tzinfo=weekly.IL_TZ)
        tue_close = datetime.combine(TUE, time(23, 0)).replace(tzinfo=weekly.IL_TZ)
        sat_futures = datetime.combine(SAT, time(9, 0)).replace(tzinfo=weekly.IL_TZ)
        prices = {
            mon_futures: {"ES=F": Decimal("5000")},
            tue_close: {"DDD": Decimal("100"), "ES=F": Decimal("5100")},
            END_TS: {"DDD": Decimal("120")},
            sat_futures: {"ES=F": Decimal("5200")},
        }
        # Bought Monday and again on Tuesday (the baseline day, so part of the
        # opening position): 15 shares valued 100 → 120.
        lots = [_lot(1, "DDD", "BUY", "10", "90", MON), _lot(2, "DDD", "BUY", "5", "101", TUE)]
        out = _run(monkeypatch, capsys, lots, prices=prices)
        assert "Start snapshot: 2026-03-03 23:00 IL" in out
        assert "End snapshot:   2026-03-06 23:00 IL" in out
        assert "Missing price data" not in out
        assert _line(out, "DDD:").startswith("DDD: $300.00 (+20.00%) | $1,500.00 → $1,800.00")

    def test_boundary_queries_filter_on_the_held_symbols(self, monkeypatch):
        seen = []
        monkeypatch.setattr(weekly, "fetch_all", lambda conn, sql, params=None: (seen.append((" ".join(sql.split()), params)), [{"ts": START_TS}])[1])
        assert weekly.snapshot_at_or_after(object(), START_TS, {"BBB", "AAA"}) == START_TS
        assert weekly.latest_snapshot(object(), {"AAA"}) == START_TS
        assert "symbol = ANY(%s)" in seen[0][0] and seen[0][1] == (START_TS, ["AAA", "BBB"])
        assert "symbol = ANY(%s)" in seen[1][0] and seen[1][1] == (["AAA"],)
        # Without symbols the old unfiltered queries remain.
        weekly.snapshot_at_or_after(object(), START_TS)
        assert "ANY" not in seen[2][0]
