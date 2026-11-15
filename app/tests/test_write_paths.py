"""The write CLIs, through their real entry points, against a fake connection
that answers SQL by what it says.

Covers audit N-2 / D-1 (a refused duplicate is reported, exit non-zero, never
"OK"), D-2 / A-3 (every symbol validated before anything is written), D-5
(more than 8 decimals refused, not rounded), F-11 (non-USD listings refused
unless --allow-non-usd), F-2 (the oversell warning for a sale with no account)
and Q-8 (income in another currency refused).

The fake runs the real ledger_inputs / oversell / ledger_writes code: only
the database is replaced, and it filters lots the way the loader's SQL does.
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from datetime import date
from decimal import Decimal

import pytest
from psycopg2 import sql as pgsql

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import add_income  # noqa: E402
import add_lot  # noqa: E402
import ledger_numbers  # noqa: E402
import sell_lot  # noqa: E402
import set_watchlist  # noqa: E402
import symbol_paths  # noqa: E402


def _text(q) -> str:
    if isinstance(q, str):
        return q
    if isinstance(q, pgsql.Composed):
        return "".join(_text(p) for p in q.seq)
    if isinstance(q, pgsql.SQL):
        return q.string
    return ""


class FakeDB:
    def __init__(self, lots=(), currency=None, lot_insert_ok=True, income_rowcount=1):
        self.lots = [dict(lot) for lot in lots]
        self.currency = currency
        self.lot_insert_ok = lot_insert_ok
        self.income_rowcount = income_rowcount
        self.statements: list[tuple[str, tuple]] = []
        self.committed = self.rolled_back = 0
        self.autocommit = False

    # psycopg2 connection surface
    def cursor(self, cursor_factory=None):
        return FakeCursor(self)

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back += 1

    def close(self):
        pass

    def inserted(self, table):
        return [p for s, p in self.statements if s.lstrip().startswith(f"INSERT INTO {table}")]


class FakeCursor:
    def __init__(self, db):
        self.db = db
        self.rows: list = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, q, params=None):
        text = " ".join(_text(q).split())
        params = tuple(params or ())
        self.db.statements.append((text, params))
        self.rows, self.rowcount = [], 0
        if text.startswith("SELECT currency FROM instruments"):
            self.rows = [{"currency": self.db.currency}] if self.db.currency else []
        elif text.startswith("INSERT INTO lots"):
            self.rowcount = 1 if self.db.lot_insert_ok else 0
            if self.db.lot_insert_ok:
                sym, acct, side, day, qty, price, fees, notes = params
                self.db.lots.append({"id": 1000 + len(self.db.lots), "symbol": sym, "account": acct,
                                     "side": side, "trade_date": day, "quantity": qty,
                                     "price": price, "fees": fees})
        elif text.startswith("INSERT INTO income"):
            self.rowcount = self.db.income_rowcount
        elif text.startswith(("INSERT INTO instruments", "UPDATE instruments")):
            self.rowcount = 1
        elif text.startswith("SELECT id, symbol, account, side, trade_date, quantity, price, fees FROM lots"):
            rows = list(self.db.lots)
            filters = [f for f in ("symbol = %s", "account = %s", "trade_date <= %s") if f in text]
            for f, value in zip(filters, params):
                col = f.split()[0]
                rows = [r for r in rows if (r[col] <= value if "<=" in f else r[col] == value)]
            self.rows = rows
        elif "FROM corporate_actions" in text:
            self.rows = []

    def fetchall(self):
        return list(self.rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None


@pytest.fixture
def db(monkeypatch):
    holder = {"db": FakeDB()}

    @contextmanager
    def fake_transaction(cfg=None):
        conn = holder["db"]
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    for mod in (add_lot, sell_lot, set_watchlist, add_income):
        monkeypatch.setattr(mod, "transaction", fake_transaction)
        monkeypatch.setattr(mod, "load_config", lambda: None)

    def use(**kw):
        holder["db"] = FakeDB(**kw)
        return holder["db"]

    return use


BASE = ["--symbol", "nvda", "--account", "IBKR", "--trade-date", "2026-03-02",
        "--qty", "10", "--price", "100"]


# ── duplicates are reported, never "OK" (N-2, D-1) ──────────────────


def test_a_refused_duplicate_is_reported_and_exits_non_zero(db, capsys):
    conn = db(lot_insert_ok=False)
    assert add_lot.main(BASE) == add_lot.EXIT_DUPLICATE
    out, err = capsys.readouterr()
    assert "OK" not in out
    assert "DUPLICATE — NOT RECORDED" in err and "NVDA" in err
    assert "combined quantity" in err      # says what to do about a second fill
    assert conn.inserted("lots")           # it did try


def test_a_recorded_lot_says_so_with_the_new_position(db, capsys):
    db()
    assert add_lot.main(BASE) == add_lot.EXIT_OK
    out = capsys.readouterr().out
    assert "OK recorded BUY 10 NVDA @ 100 on 2026-03-02 in IBKR" in out
    assert "open quantity now 10" in out


def test_sell_lot_reports_a_duplicate_too(db, capsys, monkeypatch):
    import positions
    monkeypatch.setattr(positions, "print_positions", lambda sym: pytest.fail("must not print positions"))
    db(lots=[{"id": 1, "symbol": "NVDA", "account": "IBKR", "side": "BUY",
              "trade_date": date(2026, 3, 1), "quantity": Decimal(10), "price": Decimal(90),
              "fees": Decimal(0)}], lot_insert_ok=False)
    args = [a for a in BASE]
    assert sell_lot.main(args) == add_lot.EXIT_DUPLICATE
    assert "DUPLICATE — NOT RECORDED: a SELL" in capsys.readouterr().err


def test_add_income_reports_a_duplicate(db, capsys):
    db(income_rowcount=0)
    rc = add_income.main(["--symbol", "NVDA", "--pay-date", "2026-03-15", "--amount", "4.00"])
    assert rc == 1
    out, err = capsys.readouterr()
    assert "DUPLICATE — NOT RECORDED" in err and "OK added" not in out


# ── the oversell warning on the CLI path (F-2) ───────────────────────


def test_selling_with_no_account_warns_when_the_shares_are_in_another(db, capsys, monkeypatch):
    import positions
    monkeypatch.setattr(positions, "print_positions", lambda sym: None)
    conn = db(lots=[{"id": 1, "symbol": "NVDA", "account": "IBKR", "side": "BUY",
                     "trade_date": date(2026, 3, 1), "quantity": Decimal(10), "price": Decimal(90),
                     "fees": Decimal(0)}])
    args = ["--symbol", "NVDA", "--trade-date", "2026-03-02", "--qty", "10", "--price", "100"]
    assert sell_lot.main(args) == 0
    err = capsys.readouterr().err
    assert "selling 10 NVDA in (no account)" in err and "holds 0" in err
    assert conn.inserted("lots"), "warns; still records"


# ── symbols are validated before anything is written (D-2, A-3) ─────


@pytest.mark.parametrize("bad", ["<img src=x onerror=alert(1)>", "&#60;SCRIPT&#62;", "../etc", "-X",
                                 "A B", "", "Ä", "X" * 33])
def test_every_cli_refuses_a_symbol_that_is_not_one(db, bad, capsys):
    conn = db()
    for main, argv in (
        (add_lot.main, [f"--symbol={bad}"] + BASE[2:]),
        (sell_lot.main, [f"--symbol={bad}"] + BASE[2:]),
        (set_watchlist.main, [f"--symbol={bad}"]),
        (add_income.main, [f"--symbol={bad}", "--pay-date", "2026-03-15", "--amount", "1"]),
    ):
        with pytest.raises(SystemExit) as e:
            main(argv)
        assert e.value.code == 2
    assert conn.statements == [], "nothing reached the database"
    assert "not a valid symbol" in capsys.readouterr().err or bad == ""


def test_set_watchlist_takes_the_symbols_make_passes(db, capsys):
    """`make watchlist ARGS="NVDA AMD"` passes them positionally — which the
    old --symbol/--on-only parser rejected."""
    conn = db()
    assert set_watchlist.main(["nvda", "amd"]) == 0
    updates = [p for s, p in conn.statements if s.startswith("UPDATE instruments")]
    assert updates == [(True, "NVDA"), (True, "AMD")]
    assert set_watchlist.main(["--symbol", "NVDA", "--off"]) == 0


# ── numbers (D-5) ─────────────────────────────────────────────────


@pytest.mark.parametrize("raw", ["0.123456789", "1E-9", "100.000000001"])
def test_more_than_eight_decimals_is_refused_not_rounded(raw):
    with pytest.raises(ValueError, match="more than 8 decimal places"):
        ledger_numbers.parse_quantity(raw)


@pytest.mark.parametrize("raw,expected", [("1.500000000", "1.5"), ("0.12345678", "0.12345678"), ("1E+3", "1000")])
def test_trailing_zeros_and_exponents_are_fine(raw, expected):
    assert ledger_numbers.parse_quantity(raw) == Decimal(expected)


def test_the_cli_reports_the_scale_as_a_usage_error(db, capsys):
    db()
    with pytest.raises(SystemExit) as e:
        add_lot.main(BASE[:-2] + ["--price", "100.123456789"])
    assert e.value.code == 2
    assert "more than 8 decimal places" in capsys.readouterr().err


# ── USD-only scope (F-11 writers, Q-8) ─────────────────────────────


def test_a_non_us_listing_is_refused_without_the_flag(db, capsys):
    conn = db()
    rc = add_lot.main(["--symbol", "TEVA.TA"] + BASE[2:])
    assert rc == add_lot.EXIT_REFUSED
    err = capsys.readouterr().err
    assert "REFUSED" in err and ".TA suffix" in err
    assert not conn.inserted("lots")


def test_the_flag_records_it_with_a_warning(db, capsys):
    conn = db()
    assert add_lot.main(["--symbol", "TEVA.TA", "--allow-non-usd"] + BASE[2:]) == 0
    assert "summed as if priced in dollars" in capsys.readouterr().err
    assert conn.inserted("lots")


def test_an_instrument_registered_in_another_currency_is_refused(db, capsys):
    conn = db(currency="EUR")
    assert add_lot.main(BASE) == add_lot.EXIT_REFUSED
    assert "registered as trading in EUR" in capsys.readouterr().err
    assert not conn.inserted("lots")


def test_share_classes_are_not_foreign_listings():
    for sym in ("BRK.B", "BF.B", "BRK-B", "^TA125.TA", "ES=F", "BTC-USD"):
        assert symbol_paths.foreign_listing_suffix(sym) is None, sym
    for sym, suffix in (("TEVA.TA", "TA"), ("VOD.L", "L"), ("0700.HK", "HK"), ("SHOP.TO", "TO")):
        assert symbol_paths.foreign_listing_suffix(sym) == suffix


def test_income_in_another_currency_is_refused(db, capsys):
    conn = db()
    rc = add_income.main(["--symbol", "NVDA", "--pay-date", "2026-03-15", "--amount", "4", "--currency", "eur"])
    assert rc == 2
    assert "REFUSED: income in EUR" in capsys.readouterr().err
    assert not conn.inserted("income")


# ── normalize_symbol, the single validator ─────────────────────────


@pytest.mark.parametrize("raw,expected", [(" nvda ", "NVDA"), ("brk.b", "BRK.B"), ("^gspc", "^GSPC"),
                                          ("es=f", "ES=F"), ("0700.hk", "0700.HK"), ("btc-usd", "BTC-USD")])
def test_normalize_symbol_accepts_real_tickers(raw, expected):
    assert symbol_paths.normalize_symbol(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "<B>", "A/B", "A\\B", ".HIDDEN", "-X", "NV DA", "NV\nDA", None, 7])
def test_normalize_symbol_refuses_the_rest(raw):
    with pytest.raises(ValueError):
        symbol_paths.normalize_symbol(raw)
