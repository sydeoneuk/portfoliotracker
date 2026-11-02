import io
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.importers.ii_csv import IiCsvImporter
from app.importers.t212_pie_csv import T212PieCsvImporter
from app.importers.t212_csv import _normalise_headers


class TestHoldingCsvSafety(unittest.TestCase):
    def setUp(self):
        self.db = MagicMock()
        self.platform = SimpleNamespace(id=1, account_type="ISA")

    def test_ii_rejects_partial_invalid_snapshot_before_any_write(self):
        for bad_row in ("BAD,broken,£10", ",2,£10", "BAD,nan,£10", "BAD,2,nan", "GOOD,3,£10"):
            with self.subTest(row=bad_row):
                db = MagicMock()
                importer = IiCsvImporter(db, self.platform, 1)
                result = importer.import_file(io.StringIO("Symbol,Qty,Price\nGOOD,2,£10\n" + bad_row))
                self.assertTrue(result.errors)
                self.assertEqual((result.positions_upserted, result.positions_deleted), (0, 0))
                db.execute.assert_not_called()
                db.delete.assert_not_called()
                db.commit.assert_not_called()

    def test_ii_requires_headers(self):
        result = IiCsvImporter(self.db, self.platform, 1).import_file(io.StringIO("Symbol,Qty\nABC,2\n"))
        self.assertTrue(result.errors)
        self.db.execute.assert_not_called()

    def test_ii_usd_quote_with_zero_gbp_cost_is_unknown_cost_not_a_currency_error(self):
        importer = IiCsvImporter(self.db, self.platform, 1)
        headers = ['Symbol', 'Qty', 'Price', 'Average Price', 'Book Cost']
        row = dict(zip(headers, ['ERNA', '3266', '$6.467', '£0.00', '£0.00']))
        parsed = importer._parse_row(row, {h: h.lower() for h in headers})
        self.assertEqual((parsed['currency'], parsed['current_price']), ('USD', 6.467))
        self.assertIsNone(parsed['average_price'])
        self.assertIsNone(parsed['basis']['cost_basis_amount'])
        self.assertEqual(parsed['basis']['cost_basis_status'], 'unknown')

    def test_ii_account_currency_cost_is_separate_from_quote_units(self):
        importer = IiCsvImporter(self.db, self.platform, 1)
        headers = ['Symbol', 'Qty', 'Price', 'Average Price', 'Book Cost']
        for quote in ('$6.467', '646.7p'):
            row = dict(zip(headers, ['ERNA', '10', quote, '£5', '£49']))
            parsed = importer._parse_row(row, {h: h.lower() for h in headers})
            self.assertEqual(parsed['basis']['cost_basis_amount'], 49)
            self.assertEqual(parsed['basis']['cost_basis_currency'], 'GBP')

    def test_ii_recognises_explicit_totals_footer_but_not_unlabelled_missing_holdings(self):
        importer = IiCsvImporter(self.db, self.platform, 1)
        headers = ['Symbol', 'Name', 'Qty', 'Price', 'Average Price', 'Book Cost']
        mapping = {h: h.lower() for h in headers}
        footer = dict(zip(headers, ['', '', '', 'GBP', '', '£100']))
        with self.assertRaisesRegex(ValueError, 'Missing holding symbol'):
            importer._parse_row(footer, mapping)
        self.assertIsNone(importer._parse_row(dict(zip(headers, ['', '', '', 'Totals', '', ''])), mapping))
        self.assertIsNone(importer._parse_row(footer, mapping))
        with self.assertRaisesRegex(ValueError, 'after totals'):
            importer._parse_row(dict(zip(headers, ['ERNA', 'Test', '1', '$6', '£5', '£5'])), mapping)

    def test_pie_invalid_rows_do_not_create_or_replace_pie(self):
        for rows in ("ABC,bad", "ABC,2\nBAD,nan", "ABC,2\n,3", "ABC,2\nABC,3", "Total,100"):
            with self.subTest(rows=rows):
                importer = T212PieCsvImporter(self.db, self.platform, 1, "Test")
                with patch.object(importer, "_ensure_pie") as ensure:
                    result = importer.import_file(io.StringIO("Slice,Quantity\n" + rows))
                self.assertTrue(result.errors)
                self.assertEqual((result.holdings_imported, result.holdings_replaced), (0, 0))
                ensure.assert_not_called()
        self.db.execute.assert_not_called()
        self.db.commit.assert_not_called()

    def test_ii_write_failure_rolls_back_and_resets_counts(self):
        importer = IiCsvImporter(self.db, self.platform, 1)
        self.db.query.return_value.filter_by.return_value.all.return_value = [SimpleNamespace(ticker="OLD")]
        self.db.execute.side_effect = RuntimeError("database unavailable")
        result = importer.import_file(io.StringIO("Symbol,Qty,Price\nABC,2,£10\n"))
        self.db.rollback.assert_called_once()
        self.db.commit.assert_not_called()
        self.assertEqual((result.positions_upserted, result.positions_deleted), (0, 0))

    def test_pie_reconciliation_failure_rolls_back_replacement(self):
        importer = T212PieCsvImporter(self.db, self.platform, 1, "Test")
        with patch.object(importer, "_ensure_pie", return_value=SimpleNamespace(pk=1)):
            with patch.object(importer, "_reconcile", side_effect=RuntimeError("failed reconciliation")):
                result = importer.import_file(io.StringIO("Slice,Quantity\nABC,2\n"))
        self.db.rollback.assert_called_once()
        self.db.commit.assert_not_called()
        self.assertEqual((result.holdings_imported, result.holdings_replaced), (0, 0))

    def test_pie_value_and_percentage_columns_are_independent(self):
        headers = ["Slice", "Quantity", "Price (avg. invested value)", "Price (avg. value)",
                   "Result", "Result (%)", "Target allocation (%)", "Current allocation (%)"]
        row = dict(zip(headers, ["ABC", "2", "20", "19.9", "-0.1", "-0.5", "0.5", "1"]))
        importer = T212PieCsvImporter(self.db, self.platform, 1, "Test")
        holding = importer._parse_row(row, _normalise_headers(headers), None)
        self.assertEqual(holding.price_avg_invested_value, 20)
        self.assertEqual(holding.price_avg_value, 19.9)
        self.assertEqual(holding.price_avg_result, -0.1)
        self.assertEqual(holding.price_avg_result_coef, -0.005)
        self.assertEqual(holding.expected_share, 0.005)
        self.assertEqual(holding.current_share, 0.01)
        self.db.execute.assert_not_called()
