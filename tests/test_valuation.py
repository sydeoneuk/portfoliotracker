import unittest
import sys
import time
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from app.valuation import ValuationUnavailable, convert_amount, required_rate, value_holding
from app import valuation


class TestCurrencyValuation(unittest.TestCase):
    def test_missing_price_is_listed_separately_without_substituting_cost(self):
        rows = [{"ticker": "SHEL", "currency": "GBP", "value": None, "cost": 120},
                {"ticker": "PRICED", "currency": "GBP", "value": 50}]
        priced, unavailable = valuation.split_priced_positions(rows, "GBP", {})
        self.assertEqual([p["ticker"] for p in priced], ["PRICED"])
        self.assertEqual(unavailable[0]["ticker"], "SHEL")
        self.assertIsNone(unavailable[0]["value"])
        self.assertNotIn("valuation_issue", rows[0])

    def test_missing_fx_is_an_unavailable_holding_not_zero_value(self):
        priced, unavailable = valuation.split_priced_positions(
            [{"ticker": "USD_ASSET", "currency": "USD", "value": 100}], "GBP", {})
        self.assertEqual(priced, [])
        self.assertIn("Exchange rate unavailable", unavailable[0]["valuation_issue"])

    def test_pence_and_usd_convert_once(self):
        pence = value_holding(10, 2000, "GBX", {})
        self.assertEqual(pence.value_gbp, Decimal("200"))
        usd = value_holding(2, 100, "USD", {"USD": 0.8})
        self.assertEqual(usd.value_gbp, Decimal("160"))
        self.assertEqual(usd.price_native, Decimal("100"))

    def test_display_currency_conversion(self):
        self.assertEqual(convert_amount(200, "GBP", "USD", {"USD": 0.8}), 250)
        self.assertEqual(convert_amount(2000, "GBp", "GBP", {}), 20)

    def test_missing_fx_is_never_parity(self):
        with self.assertRaises(ValuationUnavailable):
            value_holding(2, 100, "USD", {})
        with self.assertRaises(ValuationUnavailable):
            convert_amount(100, "GBP", "EUR", {})

    def test_invalid_or_missing_data_is_not_zero(self):
        for price in (None, float("nan"), float("inf"), -1):
            with self.subTest(price=price), self.assertRaises(ValuationUnavailable):
                value_holding(2, price, "GBP", {})
        for rate in (0, -1, float("nan"), float("inf")):
            with self.subTest(rate=rate), self.assertRaises(ValuationUnavailable):
                required_rate("USD", {"USD": rate})
        with self.assertRaises(ValuationUnavailable):
            value_holding(2, 100, "", {})

    def test_expired_fx_cache_is_not_used_after_provider_failure(self):
        fake_yf = SimpleNamespace(Ticker=lambda _: (_ for _ in ()).throw(RuntimeError("offline")))
        with patch.dict(valuation._fx_cache, {"USD": (0.8, time.monotonic() - 7200)}, clear=True):
            with patch.dict(sys.modules, {"yfinance": fake_yf}):
                rates = valuation.get_fx_rates_to_gbp({"USD"})
        self.assertNotIn("USD", rates)

    def test_fresh_fx_cache_avoids_network(self):
        fake_yf = SimpleNamespace(Ticker=lambda _: self.fail("Should use fresh cache"))
        with patch.dict(valuation._fx_cache, {"USD": (0.8, time.monotonic())}, clear=True):
            with patch.dict(sys.modules, {"yfinance": fake_yf}):
                self.assertEqual(valuation.get_fx_rates_to_gbp({"USD"})["USD"], 0.8)
