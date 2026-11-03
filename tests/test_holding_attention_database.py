import unittest
from unittest.mock import patch
from sqlalchemy import text
from app import holding_attention as service, ingestion, scoped_identity
from app.models import AccountListingResolution, Instrument
from tests import test_import_database as fixtures
from tests import test_scoped_identity_database as scoped_tests
from tests import test_reconciliation_database as web_tests
from tests.test_import_matching_database import Lookup


@unittest.skipUnless(fixtures.os.environ.get('TEST_DATABASE_URL'), 'requires disposable PostgreSQL')
class TestHoldingAttention(unittest.TestCase):
    setUpClass = classmethod(fixtures.TestImportDatabase.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.TestImportDatabase.tearDownClass.__func__)
    cleanup_records = fixtures.TestImportDatabase.cleanup_records
    setUp = scoped_tests.TestScopedIdentityDatabase.setUp
    client = web_tests.TestReconciliationDatabase.client

    def payload(self, **changes):
        return dict(platform_id=self.platform_id, ticker=self.ticker, isin='GB00BP6MXD84',
            currency='GBP', mic='XLON', statement_reference='Statement 2026-11-02', symbol='', **changes)

    def verify(self, lookup=None):
        return service.verify(self.db, self.user_id, **self.payload(), lookup=lookup or Lookup())

    def unprice(self):
        from app.models import Position
        self.db.query(Position).filter_by(user_id=self.user_id).update({'current_price': None})
        self.db.commit()

    def test_list_legacy_hints_are_not_treated_as_confirmed_source_evidence(self):
        self.unprice()
        result = service.attention_list(self.db, self.user_id)
        self.assertEqual(result['total'], 1)
        item = result['items'][0]
        self.assertEqual(item['isin'], '')
        self.assertEqual(item['currency'], '')
        self.assertEqual(item['hints']['isin'], 'GB00BP6MXD84')
        self.assertEqual(service.attention_list(self.db, -1)['total'], 0)

    def test_verify_then_apply_refreshes_price_without_changing_original_records(self):
        self.unprice()
        before = ingestion.state(self.db, self.user_id, self.platform_id)
        review = self.verify()
        self.assertTrue(review['can_apply'])
        self.assertEqual(self.db.query(AccountListingResolution).filter_by(user_id=self.user_id).count(), 0)
        with patch('app.account_pricing.YahooListingLookup', return_value=Lookup()):
            result = service.apply(self.db, self.user_id, review['review_token'])
        self.assertEqual(result['pricing']['status'], 'fetched')
        self.assertEqual(ingestion.state(self.db, self.user_id, self.platform_id), before)
        self.assertEqual(service.attention_list(self.db, self.user_id)['total'], 0)

    def test_missing_catalogue_units_price_only_new_market_value_in_gbp(self):
        self.instrument.currency_code = None
        self.db.commit()
        review = self.verify()
        with patch('app.account_pricing.YahooListingLookup', return_value=Lookup()):
            result = service.apply(self.db, self.user_id, review['review_token'])
        self.assertEqual(result['pricing']['status'], 'fetched')
        self.assertIsNone(self.db.get(Instrument, self.ticker).currency_code)
        from app.web.server import _fetch_filtered_positions
        row = _fetch_filtered_positions(self.db, self.user_id, 'portfolio', [])[0]
        self.assertEqual(row['currency'], 'GBP')
        self.assertEqual(row['value'], 140)
        self.assertTrue(row['cost_unknown'])
        self.assertIsNone(row['annual_rate_per_share'])
        # The snapshot now freezes the newly verified quote's explicit units.
        from app.snapshots import snapshot_csv_platforms
        from app.models import HoldingSnapshot
        snapshot_csv_platforms(self.db)
        saved = self.db.query(HoldingSnapshot).filter_by(user_id=self.user_id).one()
        self.assertEqual(saved.price_currency, 'GBP')
        self.assertEqual(saved.value_gbp, 140)
        client = self.client(self.user_id)
        self.assertEqual(client.get(f'/holding/{self.ticker}').status_code, 200)

    def test_multiple_verified_listings_need_explicit_choice_and_failed_candidates_have_no_apply(self):
        review = self.verify(Lookup(symbols=['ONE.L', 'TWO.L']))
        self.assertFalse(review['can_apply'])
        self.assertEqual(len(review['candidates']), 2)
        self.assertIsNone(review['review_token'])
        result = self.verify(Lookup(isin='US0000000001'))
        self.assertFalse(result['can_apply'])
        self.assertEqual(result['candidates'], [])

    def test_other_owner_and_foreign_tokens_are_rejected(self):
        with self.assertRaises(LookupError):
            service.verify(self.db, -1, **self.payload(), lookup=Lookup())
        review = self.verify()
        with self.assertRaises(LookupError):
            service.apply(self.db, -1, review['review_token'])

    def test_stale_review_does_not_apply_and_provider_failure_keeps_saved_match(self):
        review = self.verify()
        self.instrument.isin = 'US0000000001'
        self.db.commit()
        with self.assertRaisesRegex(ValueError, 'changed'):
            service.apply(self.db, self.user_id, review['review_token'])
        self.instrument.isin = 'GB00BP6MXD84'
        self.db.commit()
        review = self.verify()
        with patch('app.account_pricing.YahooListingLookup', return_value=Lookup(fail=True)):
            result = service.apply(self.db, self.user_id, review['review_token'])
        self.assertEqual(result['pricing']['status'], 'unavailable')
        self.assertEqual(self.db.query(AccountListingResolution).filter_by(user_id=self.user_id).count(), 1)

    def test_apply_retry_after_undo_does_not_restore_removed_match(self):
        review = self.verify()
        with patch('app.account_pricing.YahooListingLookup', return_value=Lookup()):
            result = service.apply(self.db, self.user_id, review['review_token'])
        scoped_identity.undo(self.db, self.user_id, result['audit_id'])
        retry = service.apply(self.db, self.user_id, review['review_token'])
        self.assertEqual(retry['status'], 'already_processed')
        self.assertEqual(self.db.query(AccountListingResolution).filter_by(user_id=self.user_id).count(), 0)

    def test_http_normal_owner_review_apply_and_origin_guards(self):
        self.unprice()
        client = self.client(self.user_id)
        self.assertEqual(client.get('/imports/attention/list').json()['total'], 1)
        payload = {**self.payload(), 'confirmed': False}
        self.assertEqual(client.post('/imports/attention/verify', json=payload).status_code, 400)
        payload['confirmed'] = True
        self.assertEqual(client.post('/imports/attention/verify', json=payload,
            headers={'origin': 'https://evil.invalid'}).status_code, 403)
        with patch('app.holding_attention.YahooListingLookup', return_value=Lookup()):
            response = client.post('/imports/attention/verify', json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        token = response.json()['review_token']
        with patch('app.account_pricing.YahooListingLookup', return_value=Lookup()):
            response = client.post('/imports/attention/apply', json={'review_token': token, 'reviewed': True})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['pricing']['status'], 'fetched')
        self.assertEqual(client.get('/imports/attention/list').json()['total'], 0)
        self.assertEqual(self.client().get('/imports/attention/list').status_code, 401)

    def test_statement_claim_cannot_override_retained_identity_and_changed_source_invalidates_review(self):
        old_review = self.verify()
        raw = f'Symbol,Qty,Price,ISIN,MIC\n{self.ticker},7,£13,GB00BP6MXD84,XLON\n'.encode()
        batch = ingestion.stage(self.db, self.user_id, self.platform_id, 'ii_holdings', raw,
                                'statement.csv', effective_date='2026-10-31')
        ingestion.apply(self.db, self.user_id, batch.id)
        with self.assertRaisesRegex(ValueError, 'source changed'):
            service.apply(self.db, self.user_id, old_review['review_token'])
        payload = self.payload()
        payload['isin'] = 'US0000000001'
        with self.assertRaisesRegex(ValueError, 'contradicts'):
            service.verify(self.db, self.user_id, **payload, lookup=Lookup())
