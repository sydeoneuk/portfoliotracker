import unittest
from unittest.mock import patch
from app import ingestion, import_matching, scoped_identity
from app.models import AccountListingResolution, AccountListingAudit
from tests import test_import_database as fixtures
from tests import test_reconciliation_database as web_fixtures
from tests.test_listing_reconciliation import candidate


class Lookup:
    def __init__(self, symbols=None, fail=False, **changes):
        self.symbols = symbols or ['EXACT.L']
        self.fail = fail
        self.changes = changes
        self.calls = 0

    def candidates(self, instrument):
        return self.symbols

    def inspect(self, symbol, expected_isin=None):
        self.calls += 1
        if self.fail:
            raise ValueError('offline')
        return candidate(symbol=symbol, **self.changes)


@unittest.skipUnless(fixtures.os.environ.get('TEST_DATABASE_URL'), 'requires disposable PostgreSQL')
class TestImportMatchingDatabase(unittest.TestCase):
    setUpClass = classmethod(fixtures.TestImportDatabase.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.TestImportDatabase.tearDownClass.__func__)
    cleanup_records = fixtures.TestImportDatabase.cleanup_records
    client = web_fixtures.TestReconciliationDatabase.client

    def setUp(self):
        fixtures.TestImportDatabase.setUp(self)
        self.batch = self.statement()

    def statement(self, isin='GB00BP6MXD84', date='2026-10-30', qty=10):
        raw = f'Symbol,Qty,Price,Average Price,ISIN,MIC\n{self.ticker},{qty},£15,£12,{isin},XLON\n'.encode()
        batch = ingestion.stage(self.db, self.user_id, self.platform_id, 'ii_holdings', raw,
                                'statement.csv', effective_date=date)
        self.assertEqual(batch.status, 'review', batch.review)
        return ingestion.apply(self.db, self.user_id, batch.id)

    def match(self, lookup=None, **kwargs):
        return import_matching.match_one(self.db, kwargs.get('user_id', self.user_id),
            self.batch.id, kwargs.get('ticker', self.ticker), lookup=lookup or Lookup())

    def test_exact_match_preserves_financial_data_and_reuses_without_network(self):
        before = ingestion.state(self.db, self.user_id, self.platform_id)
        result = self.match()
        self.assertEqual(result['status'], 'matched', result)
        self.assertEqual(before, ingestion.state(self.db, self.user_id, self.platform_id))
        provider = Lookup(fail=True)
        self.assertEqual(self.match(provider)['status'], 'reused')
        self.assertEqual(provider.calls, 0)
        self.assertEqual(self.db.query(AccountListingAudit).filter_by(user_id=self.user_id).count(), 1)
        self.batch = self.statement(date='2026-10-31', qty=11)
        self.assertEqual(self.match(provider)['status'], 'reused')

    def test_missing_source_and_catalogue_isin_never_uses_symbol_guess(self):
        self.batch = self.statement(isin='', date='2026-10-31')
        provider = Lookup()
        self.assertEqual(self.match(provider)['status'], 'needs_attention')
        self.assertEqual(provider.calls, 0)

    def test_ambiguity_failure_wrong_isin_and_wrong_currency_do_not_apply(self):
        for provider in (Lookup(symbols=['ONE.L', 'TWO.L']), Lookup(fail=True),
                         Lookup(isin='US0000000001'), Lookup(currency='USD')):
            with self.subTest(provider=provider.changes):
                self.assertEqual(self.match(provider)['status'], 'needs_attention')
                self.assertEqual(self.db.query(AccountListingResolution).filter_by(user_id=self.user_id).count(), 0)
                self.assertEqual(self.db.get(type(self.batch), self.batch.id).status, 'applied')

    def test_removed_match_is_not_automatically_restored(self):
        self.match()
        audit = self.db.query(AccountListingAudit).filter_by(user_id=self.user_id).one()
        scoped_identity.undo(self.db, self.user_id, audit.id)
        self.assertEqual(self.match()['status'], 'needs_attention')

    def test_cross_owner_unimported_ticker_and_superseded_statement_blocked(self):
        with self.assertRaises(LookupError):
            self.match(user_id=-1)
        with self.assertRaises(LookupError):
            self.match(ticker='FOREIGN')
        self.statement(date='2026-10-31', qty=11)
        with self.assertRaisesRegex(ValueError, 'newer statement'):
            self.match()

    def test_http_requires_owner_and_origin_and_does_not_require_admin(self):
        with self.client(self.user_id) as client, patch('app.automatic_matching.YahooListingLookup', return_value=Lookup()), \
                patch('app.account_pricing.YahooListingLookup', return_value=Lookup()):
            url = f'/imports/{self.batch.id}/matching'
            response = client.get(url)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['tickers'], [self.ticker])
            response = client.post(url, json={'ticker': self.ticker}, headers={'origin': 'https://evil.example'})
            self.assertEqual(response.status_code, 403)
            response = client.post(url, json={'ticker': self.ticker})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['status'], 'matched')
            self.assertEqual(response.json()['pricing']['status'], 'fetched')
