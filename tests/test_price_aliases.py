import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from app.prices import yfinance_provider as provider


class TestPriceAliasFanout(unittest.TestCase):
    def test_shared_yahoo_symbol_prices_every_legacy_alias_once(self):
        raw = MagicMock()
        raw.__getitem__.return_value.dropna.return_value.iloc.__getitem__.return_value = 2000
        aliases = [{'ticker': 'SHEL', 'yf_ticker': 'SHEL.L', 'currency': 'GBP'},
                   {'ticker': 'SHELl_EQ', 'yf_ticker': 'SHEL.L', 'currency': 'GBX'}]
        with patch.object(provider.yf, 'download', return_value=raw) as download, \
             patch.object(provider.yf, 'Ticker', return_value=SimpleNamespace(fast_info={'currency': 'GBp'})) as info:
            result = provider.YFinanceProvider().fetch([*aliases, aliases[0]])
        self.assertEqual({item.ticker for item in result}, {'SHEL', 'SHELl_EQ'})
        self.assertEqual(len(result), 2)
        self.assertTrue(all((item.price, item.currency, item.price_gbp) == (2000, 'GBX', 20) for item in result))
        self.assertEqual(download.call_args.args[0], ['SHEL.L'])
        info.assert_called_once_with('SHEL.L')
