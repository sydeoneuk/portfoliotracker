import datetime
import unittest
from types import SimpleNamespace

from app.enrichment.listing_lookup import assess_candidate, figi_identity_match, compatible_currency
from unittest.mock import patch


def candidate(symbol="SHEL.L", **changes):
    return {"symbol": symbol, "name": "Shell", "isin": "GB00BP6MXD84", "currency": "GBX",
            "exchange": "LSE", "mic": "XLON", "price": "2000",
            "observed_at": datetime.datetime.utcnow().isoformat(), "provider": "test", **changes}


class TestListingEvidence(unittest.TestCase):
    def test_isin_and_units_are_required_even_if_symbol_and_name_match(self):
        self.assertFalse(assess_candidate("GB00BP6MXD84", "GBP", None, candidate(isin="US0000000001"))["verified"])
        self.assertFalse(assess_candidate("GB00BP6MXD84", "GBP", None, candidate(isin=None))["verified"])
        self.assertFalse(assess_candidate("GB00BP6MXD84", "GBP", None, candidate(currency="USD"))["verified"])

    def test_pounds_and_pence_are_compatible_but_other_currencies_need_review(self):
        self.assertTrue(compatible_currency("GBP", "GBp"))
        self.assertFalse(compatible_currency("GBP", "USD"))
        self.assertTrue(assess_candidate("GB00BP6MXD84", "GBP", None, candidate())["verified"])

    def test_known_venue_conflict_and_invalid_prices_prevent_application(self):
        self.assertFalse(assess_candidate("GB00BP6MXD84", "GBP", "XNYS", candidate())["verified"])
        for value in (None, "NaN", "Infinity", "-1"):
            self.assertFalse(assess_candidate("GB00BP6MXD84", "GBP", None, candidate(price=value))["verified"])

    def test_figi_fallback_requires_exact_venue_and_symbol_for_requested_isin(self):
        rows = [{"ticker": "SHEL", "exchCode": "LN", "figi": "BBG-verified"}]
        self.assertIsNotNone(figi_identity_match("SHEL.L", "XLON", rows))
        self.assertIsNone(figi_identity_match("SHEL", "XNYS", rows))
        self.assertIsNone(figi_identity_match("DIFFERENT.L", "XLON", rows))

    def test_madrid_ordinary_listing_does_not_match_the_nyse_adr(self):
        from app.enrichment.listing_lookup import MIC
        self.assertEqual(MIC['MCE'], 'XMAD')
        ordinary = [{'ticker': 'BBVA', 'exchCode': 'SM', 'figi': 'Spanish-ordinary'}]
        adr = [{'ticker': 'BBVA', 'exchCode': 'UN', 'figi': 'US-ADR'}]
        self.assertIsNotNone(figi_identity_match('BBVA.MC', 'XMAD', ordinary))
        self.assertIsNone(figi_identity_match('BBVA', 'XNYS', ordinary))
        self.assertIsNone(figi_identity_match('BBVA.MC', 'XMAD', adr))
        self.assertIsNotNone(figi_identity_match('BBVA', 'XNYS', adr))

    def test_asx_and_milan_require_exact_exchange_and_symbol(self):
        from app.enrichment.listing_lookup import MIC
        for symbol, code, mic, figi_code, root in (
                ('BHP.AX', 'ASX', 'XASX', 'AU', 'BHP'),
                ('ENEL.MI', 'MIL', 'XMIL', 'IM', 'ENEL')):
            self.assertEqual(MIC[code], mic)
            rows = [{'ticker': root, 'exchCode': figi_code, 'figi': 'verified'}]
            self.assertIsNotNone(figi_identity_match(symbol, mic, rows))
            self.assertIsNone(figi_identity_match(symbol, mic, [
                {'ticker': root, 'exchCode': 'UN', 'figi': 'US-ADR'}]))

    def test_stockholm_share_class_delimiter_preserves_a_and_b_identity(self):
        from app.enrichment.listing_lookup import MIC
        self.assertEqual(MIC['STO'], 'XSTO')
        rows = [{'ticker': 'HMB', 'exchCode': 'SS', 'figi': 'Swedish-B-share'}]
        self.assertIsNotNone(figi_identity_match('HM-B.ST', 'XSTO', rows))
        self.assertIsNone(figi_identity_match('HM-A.ST', 'XSTO', rows))
        self.assertIsNone(figi_identity_match('HM.ST', 'XSTO', rows))
        self.assertIsNone(figi_identity_match('HM-B.ST', 'XSTO', [
            {'ticker': 'HMB', 'exchCode': 'GY', 'figi': 'German-line'}]))

    def test_london_terminal_figi_marker_matches_yahoo_uu_without_erasing_share_classes(self):
        rows = [{'ticker': 'UU/', 'exchCode': 'LN', 'figi': 'BBG000BBFLV5'}]
        self.assertIsNotNone(figi_identity_match('UU.L', 'XLON', rows))
        self.assertIsNone(figi_identity_match('UU.L', 'XNYS', rows))
        self.assertIsNone(figi_identity_match('UU-A.L', 'XLON', rows))
        for ticker in ('UU/A', 'UU/EUR', 'UU/USD'):
            self.assertIsNone(figi_identity_match('UU.L', 'XLON', [
                {'ticker': ticker, 'exchCode': 'LN', 'figi': 'other'}]))
        self.assertIsNotNone(figi_identity_match('BT-A.L', 'XLON', [
            {'ticker': 'BT/A', 'exchCode': 'LN', 'figi': 'share-class'}]))


class TestYahooIdentityFallback(unittest.TestCase):
    def inspect(self, direct_isin=None, rows=None, offline=False):
        import pandas as pd
        from types import SimpleNamespace
        from app.enrichment.listing_lookup import YahooListingLookup
        ticker = SimpleNamespace(
            get_info=lambda: {'isin': direct_isin, 'currency': 'USD', 'exchange': 'NMS'},
            get_isin=lambda: 'CA11134P1009',
            history=lambda **kwargs: pd.DataFrame({'Close': [323.86]}, index=pd.to_datetime(['2026-11-02'], utc=True)))
        lookup = YahooListingLookup()
        with patch('yfinance.Ticker', return_value=ticker), patch.object(lookup, 'figi_matches',
                side_effect=ValueError('offline') if offline else None,
                return_value=rows if rows is not None else [{'ticker': 'AVGO', 'exchCode': 'UW', 'figi': 'US-verified'}]) as figi:
            result = lookup.inspect('AVGO', expected_isin='US11135F1012')
        return result, figi.call_count

    def test_secondary_cdr_isin_resolved_only_with_exact_expected_isin_venue_ticker_proof(self):
        result, calls = self.inspect()
        self.assertEqual(calls, 1)
        self.assertEqual(result['isin'], 'US11135F1012')
        self.assertEqual(result['identity_evidence']['conflicting_secondary_isin'], 'CA11134P1009')
        self.assertEqual(result['identity_evidence']['exchange_code'], 'UW')

    def test_direct_metadata_isin_conflict_is_not_overridden(self):
        result, calls = self.inspect(direct_isin='CA11134P1009')
        self.assertEqual(calls, 0)
        self.assertEqual(result['isin'], 'CA11134P1009')
        self.assertFalse(assess_candidate('US11135F1012', 'USD', 'XNAS', result)['verified'])

    def test_wrong_venue_or_symbol_cannot_override_conflicting_secondary_lookup(self):
        for row in ({'ticker': 'AVGO', 'exchCode': 'CN', 'figi': 'Canadian-DR'},
                    {'ticker': 'OTHER', 'exchCode': 'UW', 'figi': 'different-stock'}):
            result, _ = self.inspect(rows=[row])
            self.assertEqual(result['isin'], 'CA11134P1009')
            self.assertFalse(assess_candidate('US11135F1012', 'USD', 'XNAS', result)['verified'])

    def test_figi_failure_does_not_invent_an_expected_isin_match(self):
        result, _ = self.inspect(offline=True)
        self.assertEqual(result['isin'], 'CA11134P1009')
        self.assertFalse(assess_candidate('US11135F1012', 'USD', 'XNAS', result)['verified'])

    def test_bbva_madrid_quote_resolves_secondary_colombian_isin_with_spanish_figi(self):
        import pandas as pd
        from types import SimpleNamespace
        from app.enrichment.listing_lookup import YahooListingLookup
        from app.listing_identity import checked_candidate
        ticker = SimpleNamespace(get_info=lambda: {'currency': 'EUR', 'exchange': 'MCE'},
            get_isin=lambda: 'COB13PA00019',
            history=lambda **kwargs: pd.DataFrame({'Close': [17.25]}, index=pd.to_datetime(['2026-11-02'], utc=True)))
        lookup = YahooListingLookup()
        with patch('yfinance.Ticker', return_value=ticker), patch.object(lookup, 'figi_matches', return_value=[
                {'ticker': 'BBVA', 'exchCode': 'SM', 'figi': 'Spanish-ordinary'}]):
            result = lookup.inspect('BBVA.MC', expected_isin='ES0113211835')
        self.assertEqual(result['isin'], 'ES0113211835')
        self.assertEqual(result['mic'], 'XMAD')
        self.assertEqual(result['identity_evidence']['conflicting_secondary_isin'], 'COB13PA00019')
        target = SimpleNamespace(isin='ES0113211835', currency_code='EUR', mic_code='XMAD')
        self.assertTrue(checked_candidate(target, result)['verified'])
