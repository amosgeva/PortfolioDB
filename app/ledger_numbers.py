"""One parser for every number that is written to the ledger.

Quantities, prices, fees, cash and income amounts used to be parsed with
``float()`` — in the CSV importer and as ``type=float`` on every write CLI.
``float("NaN")`` and ``float("Infinity")`` succeed, and the checks that
followed did not catch them: ``qty < 0`` is False for NaN, and so is ``qty ==
0``, so a NaN quantity passed the importer's positivity test. PostgreSQL then
did not catch it either — its ``numeric`` NaN sorts above every finite value,
so ``CHECK (quantity > 0)`` is satisfied by NaN and the row was stored.
Infinity failed later, at the NUMERIC(20,8) type, which under the old
importer aborted the transaction the rest of the file was in (audit F09).

So: every ledger number goes through ``parse_decimal``. It requires a finite
``Decimal`` (exact, not a float that rounds 0.1), enforces the sign the column
wants, and refuses anything the column type cannot hold before it reaches the
database. ``sql/migrations/003_finite_numeric_checks.sql`` adds the matching
``<> 'NaN'`` constraints as the backstop for any writer that does not come
through here.
"""

from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation

# NUMERIC(20,8): twelve integer digits, so anything at or above 10^12 overflows
# the column. Checked here so an overflow is a parse error with a reason, not
# a database error that aborts a transaction.
NUMERIC_MAX_EXCLUSIVE = Decimal("1e12")
NUMERIC_SCALE = 8


def parse_decimal(
    raw,
    *,
    field: str,
    minimum: Decimal | int | None = None,
    exclusive_minimum: bool = False,
) -> Decimal:
    """Parse ``raw`` into a finite Decimal that the ledger's columns can hold.

    Raises ValueError, with the field name in the message, for: empty input,
    unparseable text, NaN, ±Infinity, a magnitude the NUMERIC(20,8) columns
    cannot store, or a value below ``minimum`` (at or below it when
    ``exclusive_minimum``).
    """
    text = str(raw).strip() if raw is not None else ""
    if not text:
        raise ValueError(f"{field} is empty")
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        raise ValueError(f"{field} {text!r} is not a number") from None
    if value.is_nan():
        raise ValueError(f"{field} is NaN, which is not a number the ledger can hold")
    if value.is_infinite():
        raise ValueError(f"{field} is infinite, which is not a number the ledger can hold")
    if abs(value) >= NUMERIC_MAX_EXCLUSIVE:
        raise ValueError(
            f"{field} {text} is too large: the ledger stores at most 12 integer digits"
        )
    if minimum is not None:
        if exclusive_minimum and value <= minimum:
            raise ValueError(f"{field} must be greater than {minimum}, got {text}")
        if not exclusive_minimum and value < minimum:
            raise ValueError(f"{field} must be at least {minimum}, got {text}")
    return value


def parse_quantity(raw) -> Decimal:
    """Shares: finite and strictly positive. Direction lives in ``side``."""
    return parse_decimal(raw, field="quantity", minimum=0, exclusive_minimum=True)


def parse_price(raw) -> Decimal:
    """Per-share price: finite and non-negative (a zero price is a documented
    way to record a corporate action with no cash)."""
    return parse_decimal(raw, field="price", minimum=0)


def parse_fees(raw) -> Decimal:
    """Fees: finite and non-negative; blank means none."""
    text = str(raw).strip() if raw is not None else ""
    if not text:
        return Decimal(0)
    return parse_decimal(text, field="fees", minimum=0)


def parse_money(raw, *, field: str = "amount") -> Decimal:
    """A cash amount (balance, income, tax): finite and non-negative."""
    return parse_decimal(raw, field=field, minimum=0)


def _argparse_type(parser):
    """Wrap a parser so argparse reports its ValueError as a usage error."""

    def convert(text: str) -> Decimal:
        try:
            return parser(text)
        except ValueError as e:
            raise argparse.ArgumentTypeError(str(e)) from None

    convert.__name__ = parser.__name__
    return convert


# For `add_argument(type=...)`: the same rules, reported the argparse way.
quantity_arg = _argparse_type(parse_quantity)
price_arg = _argparse_type(parse_price)
fees_arg = _argparse_type(parse_fees)
money_arg = _argparse_type(parse_money)
