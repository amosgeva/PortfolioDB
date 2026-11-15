"""Which recorded corporate actions take effect — applied once, to both sides.

Audit F-3: the collector recorded vendor splits with prices adjusted and lots
not, unreviewed, and every reader applied them: a 2:1 split halved each
position's value until someone noticed. Now an unreviewed machine-recorded row
changes nothing — lots or prices — until approved.

Audit F-16: one split recorded under two dates (manual and vendor) compounded:
a historical 100 became 25. Now it is applied once.

Audit R-5: corporate_actions and holdings resolved the reporting timezone (a
settings read, possibly a database connection) at import.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import date, datetime, timezone
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import corporate_actions as ca  # noqa: E402
import holdings  # noqa: E402
import ledger_inputs  # noqa: E402
import twr  # noqa: E402
from corporate_actions import CorporateAction  # noqa: E402

D1, D2, D3, D4 = date(2026, 3, 2), date(2026, 3, 4), date(2026, 3, 5), date(2026, 3, 6)
LOT = {"id": 1, "symbol": "AAA", "account": "A", "side": "BUY", "trade_date": D1,
       "quantity": Decimal("10"), "price": Decimal("100"), "fees": Decimal("0")}
PRICES = {D1: {"AAA": 100.0}, D4: {"AAA": 50.0}}


def vendor(ex=D3, reviewed=False, lots=True, ratio="2"):
    return CorporateAction("AAA", "SPLIT", ex, Decimal(ratio), adjust_prices=True,
                           adjust_lots=lots, reviewed=reviewed, source="yfinance")


def manual(ex=D2, ratio="2", reviewed=False):
    return CorporateAction("AAA", "SPLIT", ex, Decimal(ratio), reviewed=reviewed, source="manual")


def value_on_last_day(actions):
    """Position value at the last price: what the dashboard shows."""
    ledger = ledger_inputs.prepare([LOT], actions)
    prices = ledger.price_by_day(PRICES)
    return holdings.value_series(ledger.lots, sorted(prices.items()))[-1][1]


class TestPendingReviewAppliesNothing:
    def test_an_unreviewed_vendor_split_adjusts_neither_side(self):
        """The old row adjusted prices only: 10 shares × a halved history,
        position value at half. Pending now leaves both sides raw."""
        pending = vendor(lots=False)
        ledger = ledger_inputs.prepare([LOT], [pending])
        assert ledger.lots[0]["quantity"] == Decimal("10")
        assert ledger.price_by_day(PRICES)[D1]["AAA"] == 100.0
        assert ca.price_factor([pending], D1) == 1 and ca.lot_factor([pending], D1) == 1
        assert ca.adjust_price_points([(D1, "AAA", 100.0)], [pending]) == [(D1, "AAA", 100.0)]

    def test_approved_it_applies_to_both_sides_together(self):
        """Ten pre-split shares, a 2:1, a 50 quote: twenty shares worth 1,000 —
        the money is unchanged, and the history reads 50, not a −50% day."""
        approved = vendor(reviewed=True)
        assert value_on_last_day([approved]) == 1000.0
        ledger = ledger_inputs.prepare([LOT], [approved])
        assert ledger.price_by_day(PRICES)[D1]["AAA"] == 50.0
        records = twr.build_daily_records(
            [dict(LOT, quantity=float(ledger.lots[0]["quantity"]), price=float(ledger.lots[0]["price"]),
                  fees=0.0)],
            ledger.price_by_day(PRICES), [])
        assert [g for _, g in twr.growth_curve(records)] == [1.0, 1.0]

    def test_hand_entered_rows_apply_as_entered(self):
        assert value_on_last_day([manual()]) == 1000.0

    def test_fetch_actions_returns_only_the_effective_ones(self, monkeypatch):
        monkeypatch.setattr(ca, "fetch_recorded_actions", lambda conn: [vendor(), manual(ex=date(2025, 1, 1))])
        acts = ca.fetch_actions(object())
        assert [a.source for a in acts] == ["manual"]

    def test_the_loader_reads_through_the_filter(self, monkeypatch):
        """ledger_inputs.load → fetch_actions → effective only."""
        monkeypatch.setattr(ca, "fetch_recorded_actions", lambda conn: [vendor()])
        monkeypatch.setattr(ledger_inputs, "fetch_all", lambda conn, q, p: [dict(LOT)])
        ledger = ledger_inputs.load(object())
        assert ledger.actions == () and ledger.lots[0]["quantity"] == Decimal("10")


class TestOneSplitTwoRecords:
    def test_applied_once_not_twice(self):
        """QA reproduction: manual 2:1 on D2 and vendor 2:1 on D3 turned a
        historical 100 into 25."""
        both = [manual(ex=D2), vendor(ex=D3, reviewed=True)]
        assert ca.adjust_price_by_day({D1: {"AAA": 100.0}}, both) == {D1: {"AAA": 50.0}}
        assert ledger_inputs.prepare([LOT], both).lots[0]["quantity"] == Decimal("20")
        assert ca.price_factor(both, D1) == 2

    def test_the_reviewed_record_wins_then_the_manual_one(self):
        a, b = manual(ex=D2), vendor(ex=D3, reviewed=True)
        (dropped, kept), = ca.near_duplicates([a, b])
        assert kept is b and dropped is a
        c, d = manual(ex=D3), vendor(ex=D2)                 # unreviewed vendor is pending anyway
        assert ca.effective_actions([c, d]) == [c]
        e, f = manual(ex=D3), manual(ex=D2)
        assert ca.effective_actions([e, f]) == [f]          # earlier date

    def test_a_trusted_date_after_the_cutoff_means_it_has_not_happened_yet(self):
        """Collapse first, then the as-of filter: of a manual row on D2 and an
        approved vendor row on D3, D3 is trusted, so as of D2 nothing applies."""
        both = [manual(ex=D2), vendor(ex=D3, reviewed=True)]
        assert ledger_inputs.prepare([LOT], both, as_of=D2).actions == ()

    def test_distinct_events_are_not_merged(self):
        far = [manual(ex=D2), manual(ex=date(2026, 3, 20))]           # 16 days apart
        assert ca.price_factor(far, D1) == 4
        other_ratio = [manual(ex=D2), manual(ex=D3, ratio="3")]
        assert ca.price_factor(other_ratio, D1) == 6
        none_rows = [CorporateAction("AAA", "NONE", D2, Decimal(1)), CorporateAction("AAA", "NONE", D3, Decimal(1))]
        assert ca.effective_actions(none_rows) == none_rows

    def test_the_duplicate_is_logged_once(self, caplog):
        both = [manual(ex=date(2026, 4, 1)), manual(ex=date(2026, 4, 2))]
        ca.effective_actions(both)
        ca.effective_actions(both)
        assert caplog.text.count("applying it once") == 1


def test_timestamped_price_points_still_use_the_boundary():
    """The lazy timezone lookup (R-5) keeps the ex_ts semantics."""
    act = CorporateAction("AAA", "SPLIT", D3, Decimal(2), ex_ts=datetime(2026, 3, 5, 14, 30, tzinfo=timezone.utc))
    before = datetime(2026, 3, 5, 14, 0, tzinfo=timezone.utc)
    after = datetime(2026, 3, 5, 15, 0, tzinfo=timezone.utc)
    pts = ca.adjust_price_points([(before, "AAA", 100.0), (after, "AAA", 50.0)], [act])
    assert [p for _, _, p in pts] == [50.0, 50.0]


def test_importing_the_pure_modules_does_not_read_settings():
    """R-5: a fresh interpreter in which the reporting-timezone lookup explodes
    can still import both modules; the lookup happens when a figure needs it."""
    app_dir = os.path.join(os.path.dirname(__file__), "..")
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "import reporting_tz\n"
        "def boom(*a, **k): raise SystemExit('settings read at import')\n"
        "reporting_tz.tzinfo = boom; reporting_tz.tz_name = boom\n"
        "import corporate_actions, holdings, ledger_inputs, oversell, twr\n"
        "print('ok')\n"
    ) % os.path.abspath(app_dir)
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                            env={**os.environ, "PORTFOLIODB_PASSWORD": ""})
    assert result.returncode == 0, result.stderr + result.stdout
    assert result.stdout.strip() == "ok"
