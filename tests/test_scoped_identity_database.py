import unittest
from unittest.mock import patch, PropertyMock
from sqlalchemy import text, delete
from sqlalchemy.exc import IntegrityError
from app import scoped_identity as service
from app.models import (Instrument, Position, Platform, Order, AccountListingResolution, AccountListingAudit,
    InstrumentListingLink, IngestionBatch)
from app.models.order import TradeImportObservation
from app.auth.models import User
from tests import test_import_database as fixtures
from tests import test_reconciliation_database as web_fixtures
from tests.test_listing_identity_database import Lookup


@unittest.skipUnless(fixtures.os.environ.get('TEST_DATABASE_URL'), 'requires disposable PostgreSQL')
class TestScopedIdentityDatabase(unittest.TestCase):
    setUpClass = classmethod(fixtures.TestImportDatabase.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.TestImportDatabase.tearDownClass.__func__)
    cleanup_records = fixtures.TestImportDatabase.cleanup_records
    client = web_fixtures.TestReconciliationDatabase.client

    def setUp(self):
        fixtures.TestImportDatabase.setUp(self)
        self.instrument = self.db.get(Instrument, self.ticker)
        self.instrument.isin, self.instrument.mic_code = 'GB00BP6MXD84', 'XLON'
        self.symbol = self.ticker.replace('_', '').upper() + '.L'
        self.db.add(Position(user_id=self.user_id, platform_id=self.platform_id, ticker=self.ticker,
            quantity=7, average_price=12, current_price=13))
        self.db.commit()

    def review(self, platform_id=None, namespace='holdings:legacy', **kwargs):
        claim = dict(isin='GB00BP6MXD84', currency='GBP', mic='XLON', statement_reference='Broker statement 2026-11-01')
        claim.update(kwargs)
        lookup = claim.pop('lookup', Lookup())
        return service.verify(self.db, self.user_id, platform_id or self.platform_id, namespace, self.ticker,
            claim.pop('symbol', self.symbol), lookup=lookup, **claim)

    def apply(self, review=None):
        return service.apply(self.db, self.user_id, (review or self.review())['review_token'])

    def event(self, reference, currency='GBP', isin=None, platform_id=None):
        event = Order(user_id=self.user_id, platform_id=platform_id or self.platform_id, source_namespace='t212',
            ticker=self.ticker, id=reference, execution_currency=currency)
        self.db.add(event); self.db.flush()
        if isin:
            self.db.add(TradeImportObservation(order_pk=event.pk, user_id=self.user_id,
                payload_hash=(reference + 'x' * 64)[:64], payload={'ISIN': isin, 'Currency (Price / share)': currency}))
        self.db.commit()
        return event

    def test_scoped_apply_preserves_positions_and_shared_catalogue_and_supports_undo(self):
        before = self.db.execute(text('SELECT row_to_json(p)::text FROM positions p WHERE user_id=:u'), {'u': self.user_id}).scalar_one()
        review = self.review()
        self.assertTrue(review['can_apply'])
        self.assertEqual(self.db.query(AccountListingResolution).count(), 0)
        result = self.apply(review)
        self.assertEqual(result['status'], 'applied')
        self.assertEqual(service.resolve_listing(self.db, self.platform_id, 'holdings:legacy', self.ticker).id, result['listing_id'])
        self.assertIsNone(self.db.get(InstrumentListingLink, self.ticker))
        self.assertEqual(self.instrument.isin, 'GB00BP6MXD84')
        self.assertEqual(before, self.db.execute(text('SELECT row_to_json(p)::text FROM positions p WHERE user_id=:u'), {'u': self.user_id}).scalar_one())
        service.undo(self.db, self.user_id, result['audit_id'])
        self.assertIsNone(service.resolve_listing(self.db, self.platform_id, 'holdings:legacy', self.ticker))

    def test_same_legacy_alias_can_resolve_differently_per_account_without_global_reassignment(self):
        first = self.apply()
        other = Platform(user_id=self.user_id, name='US account', platform_type='t212_csv', account_type='GIA', currency='USD')
        self.db.add(other); self.db.flush()
        self.db.add(Position(user_id=self.user_id, platform_id=other.id, ticker=self.ticker, quantity=3))
        self.db.commit()
        review = self.review(platform_id=other.id, isin='US0000000001', currency='USD', mic='XNYS', symbol=self.symbol[:-2],
            lookup=Lookup(isin='US0000000001', currency='USD', mic='XNYS', exchange='NYSE'))
        second = self.apply(review)
        self.assertNotEqual(first['listing_id'], second['listing_id'])
        self.assertEqual(self.instrument.isin, 'GB00BP6MXD84')
        report = service.audit_report(self.db, search=self.ticker)
        self.assertTrue(all(row['legacy_alias_conflict'] for row in report['items']))
        self.assertEqual(self.db.query(Position).filter_by(user_id=self.user_id).count(), 2)

    def test_sources_within_account_are_independent_and_contradictions_cannot_be_overridden(self):
        self.apply()
        self.event('trade-one', currency='USD', isin='US0000000001')
        with self.assertRaisesRegex(ValueError, 'contradicts'):
            self.review(namespace='events:t212')
        review = self.review(namespace='events:t212', isin='US0000000001', currency='USD', mic='XNYS', symbol=self.symbol[:-2],
            lookup=Lookup(isin='US0000000001', currency='USD', mic='XNYS', exchange='NYSE'))
        second = self.apply(review)
        self.assertIsNotNone(service.resolve_listing(self.db, self.platform_id, 'holdings:legacy', self.ticker))
        self.assertEqual(service.resolve_listing(self.db, self.platform_id, 'events:t212', self.ticker).id, second['listing_id'])

    def test_conflicting_retained_claims_in_one_scope_block_mapping(self):
        self.event('one', isin='GB00BP6MXD84')
        self.event('two', isin='US0000000001')
        ctx = service.context(self.db, self.platform_id, 'events:t212', self.ticker)
        self.assertTrue(ctx['problems'])
        with self.assertRaisesRegex(ValueError, 'conflicting'):
            self.review(namespace='events:t212')
        self.assertEqual(self.db.query(AccountListingResolution).filter_by(user_id=self.user_id).count(), 0)

    def test_identity_changes_invalidate_review_but_quantity_refreshes_do_not(self):
        review = self.review()
        position = self.db.get(Position, (self.ticker, self.platform_id, self.user_id))
        position.quantity = 99
        self.db.commit()
        result = self.apply(review)
        self.assertEqual(position.quantity, 99)
        self.instrument.isin = 'US0000000001'
        self.db.commit()
        self.assertIsNone(service.resolve_listing(self.db, self.platform_id, 'holdings:legacy', self.ticker))
        with self.assertRaisesRegex(ValueError, 'changed'):
            service.undo(self.db, self.user_id, result['audit_id'])
        self.db.rollback()

    def test_batch_is_restartable_and_does_not_duplicate_audits_or_reapply_after_undo(self):
        review = self.review()
        results = service.apply_batch(self.db, self.user_id, [review['review_token'], 'invalid'])['items']
        self.assertEqual([row['status'] for row in results], ['applied', 'blocked'])
        again = service.apply_batch(self.db, self.user_id, [review['review_token']])['items'][0]
        self.assertEqual(again['status'], 'already_processed')
        self.assertEqual(self.db.query(AccountListingAudit).filter_by(user_id=self.user_id).count(), 1)
        service.undo(self.db, self.user_id, results[0]['audit_id'])
        self.assertEqual(self.apply(review)['status'], 'already_processed')
        self.assertIsNone(service.resolve_listing(self.db, self.platform_id, 'holdings:legacy', self.ticker))

    def test_pending_reviews_expire_and_actor_tokens_cannot_be_shared(self):
        with patch('itsdangerous.timed.TimestampSigner.get_timestamp', return_value=1):
            old = self.review()
        with self.assertRaisesRegex(ValueError, 'expired'):
            self.apply(old)
        self.db.rollback()
        fresh = self.review()
        with self.assertRaises(LookupError):
            service.apply(self.db, self.user_id+1, fresh['review_token'])
        self.db.rollback()

    def test_nonexistent_scopes_and_archived_accounts_are_not_linked(self):
        with self.assertRaises(LookupError):
            self.review(namespace='events:ii')
        self.platform.is_active = False; self.db.commit()
        with self.assertRaisesRegex(ValueError, 'Archived'):
            self.review()

    def test_only_applied_sources_supply_claims_and_cost_currency_is_not_quote_currency(self):
        from app import ingestion
        raw = f'Symbol,Name,Qty,Price,Average Price,Book Cost\n{self.ticker},Test,10,$6,£5,£50\n'.encode()
        self.instrument.currency_code = 'USD'; self.db.commit()
        batch = ingestion.stage(self.db, self.user_id, self.platform_id, 'ii_holdings', raw, 'source.csv', effective_date='2026-10-31')
        self.assertEqual(service.context(self.db, self.platform_id, 'holdings:legacy', self.ticker)['claims']['currency'], [])
        ingestion.apply(self.db, self.user_id, batch.id)
        ctx = service.context(self.db, self.platform_id, 'holdings:ii_holdings', self.ticker)
        self.assertEqual(ctx['claims']['currency'], ['USD'])
        self.assertNotIn('GBP', ctx['claims']['currency'])
        with self.assertRaisesRegex(ValueError, 'quote units contradict'):
            self.review(namespace='holdings:ii_holdings')

    def test_admin_routes_enforce_review_and_batch_permissions(self):
        from app.config import settings
        client = self.client(self.user_id)
        self.assertEqual(self.client().get('/admin/account-identity/audit').status_code, 401)
        self.assertEqual(client.get('/admin/account-identity/audit').status_code, 403)
        email = self.db.get(User, self.user_id).email.lower()
        payload = dict(platform_id=self.platform_id, namespace='holdings:legacy', ticker=self.ticker,
            symbol=self.symbol, isin='GB00BP6MXD84', currency='GBP', mic='XLON', statement_reference='Broker statement 2026-11-01')
        with patch.object(type(settings), 'admin_email_set', new_callable=PropertyMock, return_value={email}), \
             patch('app.scoped_identity.YahooListingLookup', return_value=Lookup()):
            self.assertEqual(client.get('/admin/instruments/account-identity').status_code, 200)
            report = client.get('/admin/account-identity/audit?search='+self.ticker)
            self.assertEqual(report.status_code, 200, report.text)
            response = client.post('/admin/account-identity/verify', json=payload)
            self.assertEqual(response.status_code, 200, response.text)
            token = response.json()['review_token']
            self.assertEqual(client.post('/admin/account-identity/apply-batch', json={'review_tokens':[token],'reviewed':False}).status_code, 400)
            response = client.post('/admin/account-identity/apply-batch', json={'review_tokens':[token],'reviewed':True})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['items'][0]['status'], 'applied')
            audit_id=response.json()['items'][0]['audit_id']
            self.assertEqual(client.get('/admin/account-identity/history').status_code, 200)
            self.assertEqual(client.post('/admin/account-identity/undo',json={'audit_id':audit_id}).status_code,200)
        self.assertEqual(client.post('/admin/account-identity/apply',json={'review_token':token}).status_code,403)

    def test_explicit_account_deletion_cleans_scoped_records_without_removing_global_listing(self):
        result = self.apply()
        response = self.client(self.user_id).post(f'/settings/platforms/{self.platform_id}/delete')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.db.query(AccountListingResolution).filter_by(user_id=self.user_id).count(), 0)
        self.assertEqual(self.db.query(AccountListingAudit).filter_by(user_id=self.user_id).count(), 0)

    def test_administrator_resolution_keeps_target_owner_separate_from_acting_user(self):
        from app.models import Base
        self.apply()
        other_user=User(email=self.ticker+'other@example.invalid',provider='test',provider_id=self.ticker+'other')
        self.db.add(other_user); self.db.flush(); other_id=other_user.id
        other=Platform(user_id=other_id,name='Other owner account',platform_type='t212_csv',account_type='GIA',currency='USD')
        self.db.add(other);self.db.flush();other_account=other.id
        self.db.add(Position(user_id=other_id,platform_id=other_account,ticker=self.ticker,quantity=42));self.db.commit()
        def cleanup_other():
            self.db.rollback()
            for table in reversed(Base.metadata.sorted_tables):
                if 'user_id' in table.c:
                    self.db.execute(delete(table).where(table.c.user_id==other_id))
            self.db.execute(delete(User).where(User.id==other_id));self.db.commit()
        self.addCleanup(cleanup_other)
        review=self.review(platform_id=other_account,isin='US0000000001',currency='USD',mic='XNYS',symbol=self.symbol[:-2],
            lookup=Lookup(isin='US0000000001',currency='USD',mic='XNYS',exchange='NYSE'))
        result=self.apply(review)
        row=service.mapping(self.db,other_account,'holdings:legacy',self.ticker)
        self.assertEqual(row.user_id,other_id)
        audit=self.db.get(AccountListingAudit,result['audit_id'])
        self.assertEqual((audit.user_id,audit.actor_id),(other_id,self.user_id))
        self.assertEqual(self.db.get(Position,(self.ticker,other_account,other_id)).quantity,42)
        self.assertNotEqual(row.listing_id,service.mapping(self.db,self.platform_id,'holdings:legacy',self.ticker).listing_id)
        row.user_id=self.user_id  # the actor is valid, but does not own this account
        with self.assertRaises(IntegrityError):
            self.db.flush()
        self.db.rollback()

    def test_completed_review_receipt_can_be_retried_after_pending_review_expiry(self):
        with patch('itsdangerous.timed.TimestampSigner.get_timestamp',return_value=1):
            review=self.review()
            result=self.apply(review)
        retry=self.apply(review)
        self.assertEqual((retry['status'],retry['audit_id']),('already_processed',result['audit_id']))

    def test_source_identity_change_after_verification_blocks_pending_mapping(self):
        order=self.event('one',currency='GBP')
        review=self.review(namespace='events:t212')
        order.execution_currency='USD';self.db.commit()
        with self.assertRaisesRegex(ValueError,'changed'):
            self.apply(review)
        self.db.rollback()
        self.assertIsNone(service.mapping(self.db,self.platform_id,'events:t212',self.ticker))

    def test_clear_data_preserves_audits_and_does_not_revive_old_resolution_on_reimport(self):
        review=self.review();self.apply(review)
        response=self.client(self.user_id).post('/settings/clear-data',follow_redirects=False)
        self.assertEqual(response.status_code,303,response.text)
        self.assertIsNone(service.mapping(self.db,self.platform_id,'holdings:legacy',self.ticker))
        self.assertEqual(self.db.query(AccountListingAudit).filter_by(user_id=self.user_id).count(),1)
        self.db.add(Position(user_id=self.user_id,platform_id=self.platform_id,ticker=self.ticker,quantity=1));self.db.commit()
        self.assertIsNone(service.resolve_listing(self.db,self.platform_id,'holdings:legacy',self.ticker))
        self.assertEqual(self.apply(review)['status'],'already_processed')
        self.assertIsNone(service.mapping(self.db,self.platform_id,'holdings:legacy',self.ticker))
