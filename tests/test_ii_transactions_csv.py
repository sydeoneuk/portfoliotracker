"""
Unit tests for the ii transactions CSV importer.

Pure-function and mock-session tests only — no database required.
"""

import datetime
import io
import unittest
from unittest.mock import MagicMock, patch

from app.importers.ii_transactions_csv import (
    IiTransactionsCsvImporter,
    _classify,
    _clean,
    _na,
    _parse_amount,
    _parse_date,
    _synth_id,
)


# ── Helper tests ──────────────────────────────────────────────────────────

class TestClean(unittest.TestCase):
    def test_strips_bom(self):
        self.assertEqual(_clean("﻿Date"), "Date")

    def test_strips_whitespace(self):
        self.assertEqual(_clean("  hello  "), "hello")

    def test_multiple_boms(self):
        self.assertEqual(_clean("﻿﻿﻿Symbol"), "Symbol")


class TestNa(unittest.TestCase):
    def test_na_returns_none(self):
        self.assertIsNone(_na("n/a"))
        self.assertIsNone(_na("N/A"))
        self.assertIsNone(_na("-"))
        self.assertIsNone(_na(""))

    def test_real_value_returned(self):
        self.assertEqual(_na("TRIG"), "TRIG")


class TestParseAmount(unittest.TestCase):
    def test_gbp_prefix(self):
        self.assertAlmostEqual(_parse_amount("£1.05"), 1.05)

    def test_comma_thousands(self):
        self.assertAlmostEqual(_parse_amount("£11,110.00"), 11110.0)

    def test_na_returns_none(self):
        self.assertIsNone(_parse_amount("n/a"))

    def test_empty_returns_none(self):
        self.assertIsNone(_parse_amount(""))

    def test_plain_float(self):
        self.assertAlmostEqual(_parse_amount("247.90"), 247.90)


class TestParseDate(unittest.TestCase):
    def test_dd_mm_yyyy(self):
        dt = _parse_date("22/10/2026")
        self.assertEqual(dt, datetime.datetime(2026, 10, 22))

    def test_iso_format(self):
        dt = _parse_date("2026-10-22")
        self.assertEqual(dt, datetime.datetime(2026, 10, 22))

    def test_na_returns_none(self):
        self.assertIsNone(_parse_date("n/a"))

    def test_empty_returns_none(self):
        self.assertIsNone(_parse_date(""))


class TestClassify(unittest.TestCase):
    def test_dividend(self):
        self.assertEqual(_classify("Div 13134   THE RENEWABLES INFRASTRUCTURE GRP   ORD NPV",
                                    "TRIG", None, None, 247.90), "dividend")

    def test_div_lowercase(self):
        self.assertEqual(_classify("div 100   SOME COMPANY", "XYZ", None, None, 10.0), "dividend")

    def test_gross_interest(self):
        self.assertEqual(_classify("GROSS INTEREST", None, None, None, 1.05), "interest")

    def test_mandatory_redemption(self):
        self.assertEqual(_classify("HM TREA 0.375 2026  Mandatory Redemption",
                                    "T26A", None, None, 10420.0), "bond_redemption")

    def test_transfer_in_trf_from(self):
        self.assertEqual(_classify("Trf from Hargreaves", None, None, None, 247.90), "transfer_in")

    def test_cashback(self):
        self.assertEqual(_classify("CASHBACK37DEC25SIPP", None, None, None, 100.0), "cashback")

    def test_debit_card(self):
        self.assertEqual(_classify("Debit card payment", None, None, None, 1.0), "deposit")

    def test_trade_buy(self):
        self.assertEqual(_classify("13754 HM TREA0.2531 Del...", "TG31", "27726LDTQW7",
                                    11110.0, None), "trade_buy")

    def test_trade_sell(self):
        self.assertEqual(_classify("Sell trade desc", "FWRG", "REF123",
                                    None, 1496.14), "trade_sell")

    def test_other(self):
        self.assertEqual(_classify("Some unknown description", None, None, None, 5.0), "other")


class TestSynthId(unittest.TestCase):
    def test_stable(self):
        dt = datetime.datetime(2026, 10, 22)
        id1 = _synth_id(3, "interest", dt, 1.05)
        id2 = _synth_id(3, "interest", dt, 1.05)
        self.assertEqual(id1, id2)

    def test_includes_platform(self):
        dt = datetime.datetime(2026, 9, 30)
        id_ = _synth_id(5, "cashback", dt, 100.0)
        self.assertIn("5", id_)
        self.assertIn("cashback", id_)

    def test_different_amounts_differ(self):
        dt = datetime.datetime(2026, 10, 22)
        id1 = _synth_id(3, "interest", dt, 1.05)
        id2 = _synth_id(3, "interest", dt, 0.22)
        self.assertNotEqual(id1, id2)


# ── Importer integration (mock session) ──────────────────────────────────

def _make_importer(platform_id: int = 3) -> tuple[IiTransactionsCsvImporter, MagicMock]:
    session = MagicMock()
    session.get.return_value = None  # simulate instrument not found → will be created
    platform = MagicMock()
    platform.id = platform_id
    platform.account_type = "GIA"
    importer = IiTransactionsCsvImporter(session=session, platform=platform, user_id=1)
    return importer, session


SAMPLE_CSV = """\
Date,Settlement Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,Running Balance
30/09/2026,30/09/2026,TRIG,BBHX2H9,n/a,n/a,Div 13134   THE RENEWABLES INFRASTRUCTURE GRP   ORD NPV,n/a,n/a,£247.90,£589.72
25/10/2026,25/10/2026,n/a,n/a,n/a,n/a,GROSS INTEREST,n/a,n/a,£1.05,£6.41
22/10/2026,22/10/2026,T26A,BNNGP66,n/a,n/a,HM TREA 0.375 2026  Mandatory Redemption,n/a,n/a,"£10,420.00","£11,115.36"
03/07/2026,07/07/2026,FWRG,BMZLQ03,214,£6.9913,214 INVO MARKETF  Del    6.99 S Date 07/07/26,27726IBQHJS,"£1,496.14",n/a,£103.64
17/04/2026,17/04/2026,n/a,n/a,n/a,n/a,Trf from Hargreaves,n/a,n/a,£247.90,"£1,044.30"
17/03/2026,17/03/2026,n/a,n/a,n/a,n/a,CASHBACK37DEC25SIPP,n/a,n/a,£100.00,£101.00
"""


class TestImporterWithSampleCsv(unittest.TestCase):
    def setUp(self):
        self.importer, self.session = _make_importer()

    def _run(self, csv_text: str):
        return self.importer.import_file(io.BytesIO(csv_text.encode("utf-8")))

    def test_counts_dividends(self):
        result = self._run(SAMPLE_CSV)
        self.assertEqual(result.dividends_upserted, 1)

    def test_counts_transactions(self):
        result = self._run(SAMPLE_CSV)
        # interest + redemption + trade_buy + transfer_in + cashback = 5
        self.assertEqual(result.transactions_upserted, 5)

    def test_no_errors(self):
        result = self._run(SAMPLE_CSV)
        self.assertEqual(result.errors, [])

    def test_summary_non_empty(self):
        result = self._run(SAMPLE_CSV)
        self.assertIn("dividend", result.summary())

    def test_bom_header_tolerance(self):
        bom_csv = "﻿﻿﻿" + SAMPLE_CSV
        result = self._run(bom_csv)
        # Should still parse correctly
        self.assertEqual(result.errors, [])

    def test_blank_row_skipped(self):
        csv_with_blank = SAMPLE_CSV + "\n\n"
        result = self._run(csv_with_blank)
        # No extra errors from blank rows
        self.assertEqual(result.errors, [])


class TestDividendUpsertData(unittest.TestCase):
    """Verify that the correct values are passed to the db for a dividend row."""

    def test_dividend_reference_format(self):
        importer, session = _make_importer(platform_id=7)
        csv = (
            "Date,Settlement Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,Running Balance\n"
            "30/09/2026,30/09/2026,TRIG,BBHX2H9,n/a,n/a,"
            "Div 13134   THE RENEWABLES INFRASTRUCTURE GRP   ORD NPV,n/a,n/a,£247.90,£589.72\n"
        )
        importer.import_file(io.BytesIO(csv.encode("utf-8")))
        self.assertTrue(session.execute.called)
        # Search all execute calls for the dividend reference (instrument upsert runs first)
        all_sql = " ".join(
            c[0][0].compile(compile_kwargs={"literal_binds": True}).string
            for c in session.execute.call_args_list
        )
        self.assertIn("div_7_TRIG_2026-09-30", all_sql)

    def test_dividend_quantity_extracted(self):
        importer, session = _make_importer(platform_id=3)
        csv = (
            "Date,Settlement Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,Running Balance\n"
            "22/10/2026,22/10/2026,TG50,BMBL1F7,n/a,n/a,"
            "Div 27556   UNITED KINGDOM(GOVERNMENT OF)   0.625% SNR BDS 22/10/2050 GBP1000 'R,"
            "n/a,n/a,£86.11,£695.36\n"
        )
        result = importer.import_file(io.BytesIO(csv.encode("utf-8")))
        self.assertEqual(result.dividends_upserted, 1)
        self.assertEqual(result.errors, [])


class TestInstrumentYfTicker(unittest.TestCase):
    def test_lse_yf_ticker_set_on_instrument_upsert(self):
        importer, session = _make_importer(platform_id=3)
        csv = (
            "Date,Settlement Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,Running Balance\n"
            "30/09/2026,30/09/2026,TRIG,BBHX2H9,n/a,n/a,"
            "Div 13134   THE RENEWABLES INFRASTRUCTURE GRP   ORD NPV,n/a,n/a,£247.90,£589.72\n"
        )
        importer.import_file(io.BytesIO(csv.encode("utf-8")))
        all_sql = " ".join(
            c[0][0].compile(compile_kwargs={"literal_binds": True}).string
            for c in session.execute.call_args_list
        )
        self.assertIn("TRIG.L", all_sql)


class TestTradeUsesProvidedReference(unittest.TestCase):
    def test_trade_uses_ii_reference_not_synthesised(self):
        importer, session = _make_importer(platform_id=3)
        csv = (
            "Date,Settlement Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit,Running Balance\n"
            "03/07/2026,07/07/2026,FWRG,BMZLQ03,214,£6.9913,"
            "214 INVO MARKETF  Del    6.99 S Date 07/07/26,27726IBQHJS,\"£1,496.14\",n/a,£103.64\n"
        )
        result = importer.import_file(io.BytesIO(csv.encode("utf-8")))
        self.assertEqual(result.transactions_upserted, 1)
        all_sql = " ".join(
            c[0][0].compile(compile_kwargs={"literal_binds": True}).string
            for c in session.execute.call_args_list
        )
        self.assertIn("27726IBQHJS", all_sql)


if __name__ == "__main__":
    unittest.main()
