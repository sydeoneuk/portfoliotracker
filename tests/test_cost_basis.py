import datetime
import unittest
from decimal import Decimal
from types import SimpleNamespace

from app.cost_basis import moving_average_cost


def trade(pk, side, quantity, amount, currency="GBP", day=None):
    return SimpleNamespace(pk=pk, side=side, filled_quantity=quantity,
                           settlement_amount=amount, settlement_currency=currency,
                           filled_at=datetime.datetime(2026, 1, day or pk))


class TestMovingAverageCost(unittest.TestCase):
    def test_full_sale_then_repurchase_starts_new_cost_pool(self):
        result = moving_average_cost([trade(1, "BUY", 10, 100), trade(2, "SELL", 10, 150),
                                      trade(3, "BUY", 10, 200)])
        self.assertEqual((result.quantity, result.amount), (Decimal(10), Decimal(200)))

    def test_partial_sale_reduces_cost_proportionally_and_fees_are_not_added_twice(self):
        # £102 settlement already includes £2 acquisition fees.
        result = moving_average_cost([trade(1, "BUY", 10, 102), trade(2, "SELL", 4, 59)])
        self.assertEqual((result.quantity, result.amount), (Decimal(6), Decimal("61.2")))

    def test_later_buy_adds_to_remaining_pool(self):
        result = moving_average_cost([trade(1, "BUY", 10, 100), trade(2, "SELL", 5, 70), trade(3, "BUY", 5, 100)])
        self.assertEqual((result.quantity, result.amount), (Decimal(10), Decimal(150)))

    def test_currency_change_and_missing_currency_are_unknown(self):
        for currency in ("USD", None):
            result = moving_average_cost([trade(1, "BUY", 10, 100), trade(2, "BUY", 5, 50, currency)])
            self.assertIsNone(result.amount)

    def test_missing_opening_holdings_do_not_create_a_cost_estimate(self):
        result = moving_average_cost([trade(1, "SELL", 5, 50), trade(2, "BUY", 10, 100)])
        self.assertFalse(result.quantity_reconciled)
        self.assertIsNone(result.amount)

    def test_unknown_cost_can_reset_after_full_disposal(self):
        result = moving_average_cost([trade(1, "BUY", 10, None), trade(2, "SELL", 10, 100), trade(3, "BUY", 2, 40)])
        self.assertEqual(result.amount, 40)

    def test_missing_or_ambiguous_time_is_unknown(self):
        result = moving_average_cost([trade(1, "BUY", 10, 100, day=1), trade(2, "SELL", 5, 50, day=1)])
        self.assertIsNone(result.amount)
        event = trade(1, "BUY", 10, 100)
        event.filled_at = None
        self.assertIsNone(moving_average_cost([event]).amount)

    def test_input_order_is_not_execution_order(self):
        result = moving_average_cost([trade(3, "BUY", 10, 200), trade(2, "SELL", 10, 150), trade(1, "BUY", 10, 100)])
        self.assertEqual(result.amount, 200)

    def test_offsets_are_normalised_before_chronological_sort(self):
        from app.importers.t212_csv import _parse_dt
        self.assertEqual(_parse_dt("2026-01-01 12:00:00+01:00"), datetime.datetime(2026, 1, 1, 11))

    def test_unsupported_security_movements_cannot_be_flattened_into_cash(self):
        import io
        from unittest.mock import MagicMock
        from app.importers.t212_csv import T212CsvImporter
        db = MagicMock()
        importer = T212CsvImporter(db, SimpleNamespace(id=1, account_type="ISA"), 1)
        result = importer.import_file(io.StringIO("Action,ID\nStock split,123\n"))
        self.assertTrue(result.errors)
        db.commit.assert_not_called()
        db.rollback.assert_called_once()
