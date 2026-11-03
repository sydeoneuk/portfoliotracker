import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from app.prices import yfinance_provider as provider


class TestPriceAliasFanout(unittest.TestCase):
    def test_pence_bare_symbols_use_london_without_changing_us_symbols(self):
        from app.enrichment.ticker_mapper import build_yf_ticker_candidates
        for symbol in ['SMT', 'TRIG']:
            self.assertEqual(build_yf_ticker_candidates(symbol, symbol, None, 'GBX'), [symbol + '.L'])
        self.assertEqual(build_yf_ticker_candidates('AVGO', 'AVGO', 'NASDAQ', 'USD')[0], 'AVGO')
        self.assertEqual(build_yf_ticker_candidates('FWRG', 'FWRG', None, 'GBP')[0], 'FWRG')

    def test_revalidation_preserves_suffix_when_venue_is_missing(self):
        from app.sync.runner import SyncRunner
        instruments = [SimpleNamespace(ticker='SMT', short_name='SMT', exchange=None, currency_code='GBX', yf_ticker='SMT'),
                       SimpleNamespace(ticker='TRIG', short_name='TRIG', exchange=None, currency_code='GBP', yf_ticker='TRIG.L')]
        runner = SyncRunner.__new__(SyncRunner)
        runner.session = MagicMock()
        runner.session.query.return_value.order_by.return_value.all.return_value = instruments
        result = runner.revalidate_yf_tickers()
        self.assertEqual(instruments[0].yf_ticker, 'SMT.L')
        self.assertEqual(instruments[1].yf_ticker, 'TRIG.L')
        self.assertEqual(result['updated'], 1)

    def test_price_download_does_not_request_bare_pence_symbol(self):
        raw = MagicMock()
        raw.__getitem__.return_value.dropna.return_value.iloc.__getitem__.return_value = 1000
        with patch.object(provider.yf, 'download', return_value=raw) as download, \
             patch.object(provider.yf, 'Ticker', return_value=SimpleNamespace(fast_info={'currency': 'GBp'})):
            result = provider.YFinanceProvider().fetch([{'ticker': 'SMT', 'yf_ticker': 'SMT', 'currency': 'GBX'}])
        self.assertEqual(download.call_args.args[0], ['SMT.L'])
        self.assertEqual(result[0].currency, 'GBX')

    def test_metadata_enrichment_tries_existing_suffix_before_bare_fallback(self):
        from app.sync.runner import SyncRunner
        runner = SyncRunner.__new__(SyncRunner)
        instrument = SimpleNamespace(ticker='TRIG', short_name='TRIG', exchange=None, currency_code='GBP', yf_ticker='TRIG.L')
        yahoo = MagicMock()
        yahoo.Ticker.return_value.info = {'longName': 'Renewables Infrastructure Group'}
        with patch('app.sync.runner.time.sleep'):
            result = runner._resolve_yfinance_metadata(instrument, yahoo)
        self.assertEqual(result[0], 'TRIG.L')
        yahoo.Ticker.assert_called_once_with('TRIG.L')

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
