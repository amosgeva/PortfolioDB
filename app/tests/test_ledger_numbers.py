"""Every number written to the ledger is finite, in range, and the right sign.

`float("NaN")` parses; `NaN < 0` and `NaN == 0` are both False; and PostgreSQL's
numeric NaN satisfies `CHECK (quantity > 0)`. So a NaN quantity used to pass
every guard on the way in (audit F09). These pin the one parser all write
paths now share.
"""

from __future__ import annotations

import argparse
import os
import sys
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ledger_numbers as ln  # noqa: E402


class TestNonFinite:
    @pytest.mark.parametrize("bad", ["NaN", "nan", "-NaN", "Infinity", "inf", "-Infinity", "-inf", "1e999", "-1e999"])
    @pytest.mark.parametrize("parser", [ln.parse_quantity, ln.parse_price, ln.parse_fees, ln.parse_money])
    def test_is_refused_by_every_parser(self, parser, bad):
        with pytest.raises(ValueError):
            parser(bad)

    def test_the_reason_names_the_field(self):
        with pytest.raises(ValueError, match="quantity is NaN"):
            ln.parse_quantity("NaN")
        with pytest.raises(ValueError, match="price is infinite"):
            ln.parse_price("Infinity")


class TestBounds:
    def test_twelve_integer_digits_is_the_ceiling(self):
        assert ln.parse_price("999999999999.99999999") == Decimal("999999999999.99999999")
        with pytest.raises(ValueError, match="too large"):
            ln.parse_price("1000000000000")
        with pytest.raises(ValueError, match="too large"):
            ln.parse_price("1e12")

    def test_precision_is_kept_exactly(self):
        """Decimal, not float: 0.1 stays 0.1 and eight places survive."""
        assert ln.parse_quantity("0.12345678") == Decimal("0.12345678")
        assert str(ln.parse_price("184.10")) == "184.10"
        assert ln.parse_quantity("12.5") == 12.5


class TestSigns:
    def test_quantity_must_be_positive(self):
        with pytest.raises(ValueError, match="greater than 0"):
            ln.parse_quantity("0")
        with pytest.raises(ValueError, match="greater than 0"):
            ln.parse_quantity("-3")
        assert ln.parse_quantity("3") == 3

    def test_price_may_be_zero_but_not_negative(self):
        assert ln.parse_price("0") == 0
        with pytest.raises(ValueError, match="at least 0"):
            ln.parse_price("-0.01")

    def test_fees_blank_is_zero_and_negative_is_refused(self):
        assert ln.parse_fees("") == 0
        assert ln.parse_fees(None) == 0
        assert ln.parse_fees(" 1.25 ") == Decimal("1.25")
        with pytest.raises(ValueError, match="fees must be at least 0"):
            ln.parse_fees("-1")

    def test_money_is_non_negative_and_names_its_field(self):
        assert ln.parse_money("1000") == 1000
        with pytest.raises(ValueError, match="cash must be at least 0"):
            ln.parse_money("-5", field="cash")


class TestTextHandling:
    @pytest.mark.parametrize("bad", ["", "   ", None, "forty", "1,000", "$5"])
    def test_non_numbers_are_refused(self, bad):
        with pytest.raises(ValueError):
            ln.parse_quantity(bad)

    def test_surrounding_whitespace_is_fine(self):
        assert ln.parse_quantity(" 40 ") == 40


class TestArgparseTypes:
    def test_usage_error_not_traceback(self):
        ap = argparse.ArgumentParser()
        ap.add_argument("--qty", type=ln.quantity_arg)
        ap.add_argument("--price", type=ln.price_arg)
        ap.add_argument("--cash", type=ln.money_arg)
        assert ap.parse_args(["--qty", "2", "--price", "0", "--cash", "10.5"]).qty == 2
        for argv in (["--qty", "NaN"], ["--qty", "-1"], ["--price", "inf"], ["--cash", "-1"]):
            with pytest.raises(SystemExit):
                ap.parse_args(argv)

    def test_type_error_message_carries_the_reason(self):
        with pytest.raises(argparse.ArgumentTypeError, match="quantity is NaN"):
            ln.quantity_arg("NaN")
