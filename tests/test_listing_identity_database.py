import unittest
from unittest.mock import patch, PropertyMock
from sqlalchemy import delete, text
from app import listing_identity as service
from app.models import Instrument, Position, InstrumentListingLink, ListingIdentityAudit, Listing
from tests import test_import_database as fixtures
from tests import test_reconciliation_database as web_fixtures
from tests.test_listing_reconciliation import candidate


class Lookup:
    def __init__(self, **changes):
        self.changes = changes

    def inspect(self, symbol, expected_isin=None):
        return candidate(symbol=symbol, **self.changes)


@unittest.skipUnless(fixtures.os.environ.get('TEST_DATABASE_URL'), 'requires disposable PostgreSQL')
class TestListingIdentityDatabase(unittest.TestCase):
    setUpClass = classmethod(fixtures.TestImportDatabase.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.TestImportDatabase.tearDownClass.__func__)
    cleanup_records = fixtures.TestImportDatabase.cleanup_records
    client = web_fixtures.TestReconciliationDatabase.client

    def setUp(self):
        fixtures.TestImportDatabase.setUp(self)
        self.instrument = self.db.get(Instrument, self.ticker)
        self.instrument.isin, self.instrument.mic_code = 'GB00BP6MXD84', 'XLON'
        self.instrument.yf_ticker = self.ticker.replace('_', '').upper() + '.L'
        self.db.add(Position(user_id=self.user_id, platform_id=self.platform_id, ticker=self.ticker,
            quantity=7, average_price=12, current_price=13))
        self.db.commit()
        def cleanup_links():
            self.db.rollback()
            self.db.execute(delete(InstrumentListingLink).where(InstrumentListingLink.ticker.in_([self.ticker, *getattr(self, 'extra_instruments', [])])))
            self.db.commit()
        self.addCleanup(cleanup_links)

    def review(self, ticker=None, symbol=None, **changes):
        return service.verify(self.db, self.user_id, ticker or self.ticker,
            symbol or self.instrument.yf_ticker, lookup=Lookup(**changes))

    def apply(self, review=None):
        return service.apply(self.db, self.user_id, (review or self.review())['review_token'])

    def test_verified_link_creates_stable_ids_and_keeps_all_legacy_values(self):
        before = self.db.execute(text('SELECT row_to_json(p)::text FROM positions p WHERE user_id=:u'), {'u': self.user_id}).scalar_one()
        metadata = service.source_metadata(self.instrument)
        review = self.review()
        self.assertTrue(review['can_apply'])
        self.assertIsNone(self.db.get(InstrumentListingLink, self.ticker))
        applied = self.apply(review)
        listing = self.db.get(Listing, applied['listing_id'])
        self.assertEqual((listing.mic, listing.quote_currency, float(listing.unit_scale)), ('XLON', 'GBP', .01))
        self.assertEqual(metadata, service.source_metadata(self.instrument))
        self.assertEqual(before, self.db.execute(text('SELECT row_to_json(p)::text FROM positions p WHERE user_id=:u'), {'u': self.user_id}).scalar_one())
        self.assertEqual(service.audit_report(self.db, search=self.ticker)['items'][0]['status'], 'verified')
        self.assertEqual(service.resolve_listing(self.db, self.ticker).id, applied['listing_id'])

    def test_two_aliases_share_listing_without_consolidating_positions(self):
        first = self.apply()
        alias = self.ticker + '_EQ'
        self.extra_instruments = [alias]
        self.db.add(Instrument(ticker=alias, isin=self.instrument.isin, mic_code='XLON', currency_code='GBX', yf_ticker=self.instrument.yf_ticker))
        self.db.add(Position(user_id=self.user_id, platform_id=self.platform_id, ticker=alias, quantity=2))
        self.db.commit()
        second = self.apply(self.review(ticker=alias))
        self.assertEqual(first['listing_id'], second['listing_id'])
        self.assertEqual(first['security_id'], second['security_id'])
        self.assertEqual(self.db.query(Position).filter_by(user_id=self.user_id).count(), 2)
        self.assertEqual(self.db.get(Position, (self.ticker, self.platform_id, self.user_id)).quantity, 7)

    def test_isin_alone_and_wrong_currency_cannot_verify_listing(self):
        for changes in ({'mic': None}, {'mic': 'XNYS'}, {'isin': 'US0000000001'}, {'currency': 'USD'}):
            with self.subTest(changes=changes):
                review = self.review(**changes)
                self.assertFalse(review['can_apply'])
                self.assertIsNone(review['review_token'])

    def test_changed_metadata_invalidates_review_and_existing_link(self):
        review = self.review()
        self.instrument.exchange = 'Changed'
        self.db.commit()
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.apply(review)
        self.db.rollback()
        applied = self.apply()
        self.instrument.currency_code = 'USD'
        self.db.commit()
        self.assertEqual(service.audit_report(self.db, search=self.ticker)['items'][0]['status'], 'stale')
        self.assertIsNone(service.resolve_listing(self.db, self.ticker))
        with self.assertRaisesRegex(ValueError, 'newer state'):
            service.undo(self.db, self.user_id, applied['audit_id'])
        self.db.rollback()

    def test_undo_retains_stable_records_and_rejects_old_audits(self):
        first = self.apply()
        second = self.apply()
        with self.assertRaises(ValueError):
            service.undo(self.db, self.user_id, first['audit_id'])
        self.db.rollback()
        service.undo(self.db, self.user_id, second['audit_id'])
        self.assertIsNotNone(self.db.get(Listing, first['listing_id']))
        self.assertGreater(self.db.get(InstrumentListingLink, self.ticker).revision, 2)
        self.assertEqual(self.db.query(ListingIdentityAudit).filter_by(user_id=self.user_id).count(), 3)

    def test_signed_review_is_actor_scoped_and_expires(self):
        review = self.review()
        self.assertEqual(self.review(symbol=self.instrument.yf_ticker.lower())['candidate']['symbol'], self.instrument.yf_ticker)
        with self.assertRaises(LookupError):
            service.apply(self.db, self.user_id + 1, review['review_token'])
        self.db.rollback()
        with patch('itsdangerous.timed.TimestampSigner.get_timestamp', return_value=1):
            old = self.review()
        with self.assertRaisesRegex(ValueError, 'expired'):
            self.apply(old)
        self.db.rollback()

    def test_provider_symbol_cannot_be_reassigned_to_different_listing(self):
        self.apply()
        self.instrument.mic_code = None
        self.db.commit()
        review = self.review(mic='XNYS')
        self.assertTrue(review['can_apply'])
        with self.assertRaisesRegex(ValueError, 'different listing'):
            self.apply(review)
        self.db.rollback()

    def test_admin_routes_enforce_permissions_review_apply_and_undo(self):
        from app.config import settings
        from app.auth.models import User
        client = self.client(self.user_id)
        self.assertEqual(self.client().get('/admin/listing-identity/audit').status_code, 401)
        self.assertEqual(client.get('/admin/listing-identity/audit').status_code, 403)
        email = self.db.get(User, self.user_id).email.lower()
        with patch.object(type(settings), 'admin_email_set', new_callable=PropertyMock, return_value={email}), \
             patch('app.listing_identity.YahooListingLookup', return_value=Lookup()):
            self.assertEqual(client.get('/admin/instruments/identity').status_code, 200)
            self.assertEqual(client.get('/admin/listing-identity/audit?search=' + self.ticker).status_code, 200)
            response = client.post('/admin/listing-identity/verify', json={'ticker': self.ticker, 'symbol': self.instrument.yf_ticker})
            self.assertEqual(response.status_code, 200, response.text)
            token = response.json()['review_token']
            response = client.post('/admin/listing-identity/apply', json={'review_token': token})
            self.assertEqual(response.status_code, 200, response.text)
            audit = response.json()['audit_id']
            self.assertEqual(client.get('/admin/listing-identity/history').status_code, 200)
            self.assertEqual(client.post('/admin/listing-identity/undo', json={'audit_id': audit}).status_code, 200)
        self.assertEqual(client.post('/admin/listing-identity/apply', json={'review_token': token}).status_code, 403)

    def test_new_source_alias_stays_unresolved_until_verified(self):
        self.assertIsNone(service.resolve_listing(self.db, self.ticker))
        original = self.db.execute(text('SELECT COUNT(*) FROM instrument_listing_links')).scalar_one()
        result = service.audit_summary(self.db)
        self.assertGreater(result['unresolved'], 0)
        self.assertEqual(original, self.db.execute(text('SELECT COUNT(*) FROM instrument_listing_links')).scalar_one())

    def test_same_security_on_another_venue_remains_a_separate_listing(self):
        first = self.apply()
        alias = self.ticker + '_US'
        self.extra_instruments = [alias]
        symbol = self.instrument.yf_ticker.removesuffix('.L')
        self.db.add(Instrument(ticker=alias, isin=self.instrument.isin, mic_code='XNYS', currency_code='USD', yf_ticker=symbol))
        self.db.commit()
        second = self.apply(self.review(ticker=alias, symbol=symbol, mic='XNYS', currency='USD', exchange='NYSE'))
        self.assertEqual(first['security_id'], second['security_id'])
        self.assertNotEqual(first['listing_id'], second['listing_id'])
