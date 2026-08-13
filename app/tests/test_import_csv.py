"""CSV importer parsing rules.

The bug these exist for: `side` used to be hardcoded to 'BUY', so importing a
trade history with sales produced overstated positions and zero realized P&L
with nothing on screen suggesting anything had gone wrong.
"""

from __future__ import annotations

from datetime import date

import pytest

from import_csv_history import (
    infer_account,
    parse_quantity,
    parse_side,
    parse_trade_date,
)


class TestSide:
    def test_absent_means_buy(self):
        """A holdings export has no direction column, and every row in one is a
        purchase. Defaulting keeps those files importing unchanged."""
        assert parse_side(None) == "BUY"
        assert parse_side("") == "BUY"
        assert parse_side("   ") == "BUY"

    @pytest.mark.parametrize("word", ["BUY", "buy", "B", "bot", "Bought", "PURCHASE"])
    def test_buy_synonyms(self, word):
        assert parse_side(word) == "BUY"

    @pytest.mark.parametrize("word", ["SELL", "sell", "S", "sld", "Sold", "SALE"])
    def test_sell_synonyms(self, word):
        assert parse_side(word) == "SELL"

    def test_unknown_word_is_refused_not_guessed(self):
        """'SHORT' is neither, and treating it as a sale would silently invent a
        position the ledger cannot represent (shorts are unsupported)."""
        with pytest.raises(ValueError, match="unrecognised Side"):
            parse_side("SHORT")


class TestQuantity:
    def test_positive_passes_through(self):
        assert parse_quantity("40") == 40.0
        assert parse_quantity(" 12.5 ") == 12.5

    def test_negative_is_refused_with_the_fix_in_the_message(self):
        """Several brokers encode a sale as a negative quantity — but so do
        shorts, assignments and corrections, so this refuses and says what to do
        instead of guessing."""
        with pytest.raises(ValueError) as e:
            parse_quantity("-40")
        assert "Side column with SELL" in str(e.value)
        assert "will not guess" in str(e.value)

    def test_zero_is_refused(self):
        with pytest.raises(ValueError, match="quantity is 0"):
            parse_quantity("0")

    def test_unparseable_raises(self):
        with pytest.raises(ValueError):
            parse_quantity("forty")


class TestAccountInference:
    def test_tagged_account_in_comment_wins(self):
        assert infer_account("IBKR core sleeve", "BrokerA", ["IBKR"]) == "IBKR"

    def test_match_is_case_insensitive(self):
        assert infer_account("bought in ibkr", "BrokerA", ["IBKR"]) == "IBKR"

    def test_falls_back_to_default(self):
        assert infer_account("opening position", "BrokerA", ["IBKR"]) == "BrokerA"
        assert infer_account(None, "BrokerA", ["IBKR"]) == "BrokerA"

    def test_first_tag_wins_when_a_comment_names_two(self):
        """Deterministic rather than arbitrary: the order you pass
        --tagged-accounts decides."""
        assert infer_account("IBKR to BrokerB transfer", "X", ["IBKR", "BrokerB"]) == "IBKR"


class TestTradeDate:
    def test_compact_form(self):
        assert parse_trade_date("20260213") == date(2026, 2, 13)
        assert parse_trade_date(" 20260213 ") == date(2026, 2, 13)

    def test_other_formats_are_refused(self):
        """A slashed date would be ambiguous between US and ISO ordering, and
        guessing wrong moves a trade by months."""
        with pytest.raises(ValueError):
            parse_trade_date("2026/02/13")
