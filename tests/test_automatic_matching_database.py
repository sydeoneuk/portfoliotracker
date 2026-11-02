import unittest
from unittest.mock import patch
from app import automatic_matching as auto, scoped_identity, ingestion
from app.models import Instrument, Position, AccountListingResolution
from tests import test_import_database as fixtures
from tests import test_scoped_identity_database as scoped_tests
from tests import test_reconciliation_database as web_tests
from tests.test_import_matching_database import Lookup


class AaifLookup:
    def __init__(self, wrong_isin=False, ambiguous=False):
        self.calls = []
        self.wrong_isin, self.ambiguous = wrong_isin, ambiguous

    def candidates(self, target):
        return ['AAIF', 'AAIF.L'] + (['ANOTHER.L'] if self.ambiguous else [])

    def inspect(self, symbol, expected_isin=None):
        self.calls.append((symbol, expected_isin))
        if symbol == 'AAIF':
            raise ValueError('Invalid bare provider symbol')
        from tests.test_listing_reconciliation import candidate
        return candidate(symbol=symbol, isin='US0000000001' if self.wrong_isin else 'GB00B0P6J834', price='288')


@unittest.skipUnless(fixtures.os.environ.get('TEST_DATABASE_URL'), 'requires disposable PostgreSQL')
class TestAutomaticMatching(unittest.TestCase):
    setUpClass = classmethod(fixtures.TestImportDatabase.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.TestImportDatabase.tearDownClass.__func__)
    cleanup_records = fixtures.TestImportDatabase.cleanup_records
    setUp = scoped_tests.TestScopedIdentityDatabase.setUp
    client = web_tests.TestReconciliationDatabase.client

    def match(self, lookup=None, user_id=None):
        return auto.match_holding(self.db, user_id or self.user_id, self.platform_id, self.ticker, lookup or Lookup())

    def aaif(self):
        self.instrument.isin = 'GB00B0P6J834'
        self.instrument.currency_code = self.instrument.exchange = self.instrument.mic_code = None
        self.instrument.yf_ticker = 'AAIF'
        alias = self.ticker + '_alias'
        self.extra_instruments = [alias]
        self.db.add(Instrument(ticker=alias, isin='GB00B0P6J834', currency_code='GBX', exchange='LSE', yf_ticker='AAIF.L'))
        self.db.commit()

    def test_aaif_legacy_isin_and_sibling_hint_verify_automatically_despite_bad_bare_symbol(self):
        self.aaif()
        before = ingestion.state(self.db, self.user_id, self.platform_id)
        lookup = auto.CachedLookup(AaifLookup())
        result = self.match(lookup)
        self.assertEqual(result['status'], 'matched', result)
        row = self.db.query(AccountListingResolution).filter_by(user_id=self.user_id).one()
        self.assertEqual(row.claim['isin'], 'GB00B0P6J834')
        self.assertEqual(row.claim['currency'], 'GBX')
        self.assertIn('Automatic provider validation', row.claim['statement_reference'])
        self.assertEqual(row.evidence['provider']['symbol'], 'AAIF.L')
        from app.account_pricing import refresh_resolution
        self.assertEqual(refresh_resolution(self.db, self.user_id, row.id, lookup)['status'], 'fetched')
        self.assertEqual(before, ingestion.state(self.db, self.user_id, self.platform_id))
        from app.holding_attention import attention_list
        self.assertEqual(attention_list(self.db, self.user_id)['total'], 0)
        self.assertEqual(lookup.lookup.calls.count(('AAIF.L', 'GB00B0P6J834')), 1)

    def test_catalogue_isin_requires_independent_exact_provider_match(self):
        self.assertEqual(self.match(Lookup(isin='US0000000001'))['status'], 'needs_attention')
        self.assertEqual(self.db.query(AccountListingResolution).filter_by(user_id=self.user_id).count(), 0)

    def test_broker_symbol_selects_exact_currency_line_when_isin_has_two_verified_listings(self):
        from tests.test_listing_reconciliation import candidate
        alias = 'USDV'
        self.extra_instruments = [alias]
        self.db.add(Instrument(ticker=alias, isin='IE00B6YX5D40', currency_code=None, yf_ticker='USDV'))
        self.db.flush()
        self.db.add(Position(user_id=self.user_id, platform_id=self.platform_id, ticker=alias, quantity=2))
        self.db.commit()
        class Lines:
            def candidates(self, target):
                return ['UDVD.L', 'USDV.L']
            def inspect(self, symbol, expected_isin=None):
                if symbol == 'USDV':
                    raise ValueError('No bare provider symbol')
                return candidate(symbol=symbol, isin='IE00B6YX5D40', currency='USD' if symbol == 'UDVD.L' else 'GBP')
        result = auto.match_holding(self.db, self.user_id, self.platform_id, alias, Lines())
        self.assertEqual(result['status'], 'matched', result)
        row = self.db.query(AccountListingResolution).filter_by(user_id=self.user_id, ticker=alias).one()
        self.assertEqual(row.evidence['provider']['symbol'], 'USDV.L')
        self.assertEqual(row.claim['currency'], 'GBP')
        # A retained price currency remains stronger than a symbol preference.
        raw = b'Symbol,Qty,Price,ISIN,MIC\nUSDV,2,$12,IE00B6YX5D40,XLON\n'
        batch = ingestion.stage(self.db, self.user_id, self.platform_id, 'ii_holdings', raw,
                                'statement.csv', effective_date='2026-10-31')
        ingestion.apply(self.db, self.user_id, batch.id)
        result = auto.match_holding(self.db, self.user_id, self.platform_id, alias, Lines())
        self.assertEqual(result['status'], 'matched', result)
        row = self.db.query(AccountListingResolution).filter_by(user_id=self.user_id,
            ticker=alias, source_namespace='holdings:ii_holdings').one()
        self.assertEqual(row.evidence['provider']['symbol'], 'UDVD.L')
        self.assertEqual(row.claim['currency'], 'USD')

    def test_missing_quote_units_come_from_provider_not_guessed_from_book_cost(self):
        self.instrument.currency_code = None
        self.db.commit()
        result = self.match()
        self.assertEqual(result['status'], 'matched')
        self.assertEqual(self.db.query(AccountListingResolution).filter_by(user_id=self.user_id).one().claim['currency'], 'GBX')
        self.assertIsNone(self.db.get(Instrument, self.ticker).currency_code)

    def test_ambiguous_verified_listings_and_wrong_hint_do_not_apply(self):
        self.aaif()
        for lookup in (AaifLookup(ambiguous=True), AaifLookup(wrong_isin=True)):
            self.assertEqual(self.match(lookup)['status'], 'needs_attention')
        self.assertEqual(self.db.query(AccountListingResolution).filter_by(user_id=self.user_id).count(), 0)

    def test_no_isin_no_provider_call_and_other_owner_cannot_match(self):
        self.instrument.isin = None
        self.db.commit()
        lookup = Lookup()
        self.assertEqual(self.match(lookup)['status'], 'needs_attention')
        self.assertEqual(lookup.calls, 0)
        with self.assertRaises(LookupError):
            self.match(user_id=-1)

    def test_retained_source_claim_is_authoritative_and_not_overridden_by_catalogue(self):
        raw = f'Symbol,Qty,Price,ISIN,MIC\n{self.ticker},7,$13,US0000000001,XNYS\n'.encode()
        self.instrument.currency_code = 'USD'
        self.db.commit()
        batch = ingestion.stage(self.db, self.user_id, self.platform_id, 'ii_holdings', raw,
                                'statement.csv', effective_date='2026-10-31')
        ingestion.apply(self.db, self.user_id, batch.id)
        self.assertEqual(self.match(Lookup())['status'], 'needs_attention')
        result = self.match(Lookup(symbols=[self.symbol + '-US'], isin='US0000000001', currency='USD', mic='XNYS', exchange='NYSE'))
        self.assertEqual(result['status'], 'matched', result)

    def test_removed_match_not_restored_and_reuse_does_not_reinspect_provider(self):
        result = self.match()
        lookup = Lookup(fail=True)
        self.assertEqual(self.match(lookup)['status'], 'reused')
        self.assertEqual(lookup.calls, 0)
        from app.models import AccountListingAudit
        audit = self.db.query(AccountListingAudit).filter_by(user_id=self.user_id).one()
        scoped_identity.undo(self.db, self.user_id, audit.id)
        self.assertEqual(self.match()['status'], 'needs_attention')

    def test_http_auto_repairs_owner_holding_without_statement_confirmation(self):
        self.aaif()
        client = self.client(self.user_id)
        with patch('app.automatic_matching.YahooListingLookup', return_value=AaifLookup()):
            response = client.post('/imports/attention/auto', json={'platform_id': self.platform_id, 'ticker': self.ticker})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['status'], 'matched')
        self.assertEqual(response.json()['pricing']['status'], 'fetched')
        self.assertEqual(client.post('/imports/attention/auto', json={'platform_id': self.platform_id, 'ticker': self.ticker},
            headers={'origin': 'https://evil.invalid'}).status_code, 403)

    def test_refresh_automatically_matches_then_prices_legacy_holdings(self):
        self.aaif()
        self.db.query(Position).filter_by(user_id=self.user_id).update({'current_price': None})
        self.db.commit()
        from app.account_pricing import refresh_due_prices
        with patch('app.account_pricing.YahooListingLookup', return_value=AaifLookup()):
            result = refresh_due_prices(self.db, self.user_id)
        self.assertEqual(result['automatically_matched'], 1, result)
        self.assertEqual(result['fetched'], 1, result)
