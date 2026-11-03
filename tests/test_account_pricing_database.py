import datetime as dt
from decimal import Decimal
import unittest
from unittest.mock import patch
from sqlalchemy import text
from app import account_pricing as prices, ingestion
from app.models import AccountListingPrice, Instrument, Position, Platform
from app.valuation import effective_price_sql
from tests import test_import_database as fixtures
from tests import test_scoped_identity_database as scoped_tests
from tests.test_listing_identity_database import Lookup


@unittest.skipUnless(fixtures.os.environ.get('TEST_DATABASE_URL'), 'requires disposable PostgreSQL')
class TestAccountPricingDatabase(unittest.TestCase):
    setUpClass = classmethod(fixtures.TestImportDatabase.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.TestImportDatabase.tearDownClass.__func__)
    cleanup_records = fixtures.TestImportDatabase.cleanup_records
    setUp = scoped_tests.TestScopedIdentityDatabase.setUp
    review = scoped_tests.TestScopedIdentityDatabase.review
    apply = scoped_tests.TestScopedIdentityDatabase.apply

    def price(self, platform_id=None, legacy=False):
        return self.db.execute(text(f'SELECT {effective_price_sql(use_account_prices=not legacy)} FROM positions p '
            'WHERE p.user_id=:u AND p.platform_id=:p AND p.ticker=:t'),
            {'u': self.user_id, 'p': platform_id or self.platform_id, 't': self.ticker}).scalar_one()

    def refresh(self, result, **kwargs):
        return prices.refresh_resolution(self.db, self.user_id, self.resolution_id(result), **kwargs)

    def resolution_id(self, result):
        from app.models import AccountListingResolution
        return self.db.query(AccountListingResolution).filter_by(user_id=self.user_id,
            platform_id=result['platform_id'], ticker=self.ticker).one().id

    def test_verified_pence_quote_converts_once_and_never_changes_cost_or_legacy_rows(self):
        before = ingestion.state(self.db, self.user_id, self.platform_id)
        self.assertEqual(self.price(), 13)
        result = self.apply()
        self.assertIsNone(self.price())  # no fallback to a different legacy listing
        self.assertEqual(self.refresh(result, lookup=Lookup())['status'], 'fetched')
        self.assertEqual(self.price(), 20)
        self.assertEqual(self.price(legacy=True), 13)
        quote = self.db.get(AccountListingPrice, self.resolution_id(result))
        self.assertEqual(quote.raw_price, 2000)
        self.assertEqual(quote.quote_currency, 'GBX')
        self.assertEqual(quote.unit_scale, Decimal('.01'))
        self.assertEqual(quote.conversion_rate, Decimal('.01'))
        self.assertEqual(before, ingestion.state(self.db, self.user_id, self.platform_id))

    def test_accounts_with_same_alias_receive_independent_prices_and_weighted_combined_price(self):
        first = self.apply()
        self.refresh(first, lookup=Lookup())
        other = Platform(user_id=self.user_id, name='Other', platform_type='interactive_investor', account_type='ISA', currency='GBP')
        self.db.add(other); self.db.flush()
        self.db.add(Position(user_id=self.user_id, platform_id=other.id, ticker=self.ticker, quantity=3, current_price=15))
        self.db.commit()
        second = self.apply(self.review(platform_id=other.id, symbol=self.symbol + 'X', lookup=Lookup(price='3000')))
        self.refresh(second, lookup=Lookup(price='3000'))
        self.assertEqual(self.price(other.id), 30)
        self.assertEqual(self.price(), 20)
        from app.web.server import _COMBINED_SQL, _PLATFORM_SQL, _ACCOUNT_TYPE_SQL, _build_multi_platform_sql
        params = {'user_id': self.user_id, 'platform_id': self.platform_id, 'account_type': 'ISA'}
        for query, expected in ((_COMBINED_SQL, 23), (_PLATFORM_SQL, 20), (_ACCOUNT_TYPE_SQL, 23),
                                (_build_multi_platform_sql([self.platform_id, other.id]), 23)):
            row = self.db.execute(query, params).mappings().one()
            self.assertEqual(float(row['current_price']), expected)
            self.assertEqual(float(row['value']), expected * (7 if expected == 20 else 10))

    def test_foreign_quote_uses_explicit_fx_and_missing_fx_never_assumes_parity(self):
        result = self.apply(self.review(isin='US0000000001', currency='USD', mic='XNYS', symbol='EXACT',
            lookup=Lookup(isin='US0000000001', currency='USD', mic='XNYS', exchange='NYSE', price='100')))
        lookup = Lookup(isin='US0000000001', currency='USD', mic='XNYS', exchange='NYSE', price='100')
        with patch('app.account_pricing.get_fx_rates_to_gbp', return_value={'USD': .8}):
            self.assertEqual(self.refresh(result, lookup=lookup)['status'], 'fetched')
        self.assertEqual(self.price(), 80)  # display contract stays GBP
        quote = self.db.get(AccountListingPrice, self.resolution_id(result))
        quote.expires_at = dt.datetime.utcnow() - dt.timedelta(seconds=1)
        self.db.commit()
        with patch('app.account_pricing.get_fx_rates_to_gbp', return_value={}):
            result = self.refresh(result, lookup=lookup)
        self.assertIn('Exchange rate unavailable', result['reason'])
        self.assertIsNone(self.price())

    def test_expired_quote_and_changed_units_are_unavailable_without_legacy_fallback(self):
        result = self.apply()
        self.refresh(result, lookup=Lookup())
        quote = self.db.get(AccountListingPrice, self.resolution_id(result))
        quote.expires_at = dt.datetime.utcnow() - dt.timedelta(seconds=1)
        self.db.commit()
        self.assertIsNone(self.price())
        failed = self.refresh(result, lookup=Lookup(currency='GBP'), force=True)
        self.assertEqual(failed['status'], 'unavailable')
        self.assertIsNone(self.price())
        rows = prices.decorate_valuation_issues(self.db, self.user_id, [{'ticker': self.ticker, 'value': None}])
        self.assertIn('units or exchange changed', rows[0]['valuation_issue'])

    def test_changed_catalogue_and_mapping_revision_invalidate_saved_quote(self):
        result = self.apply()
        self.refresh(result, lookup=Lookup())
        self.instrument.isin = 'US0000000001'
        self.db.commit()
        self.assertIsNone(self.price())
        self.assertEqual(self.refresh(result, lookup=Lookup())['status'], 'unavailable')

    def test_wrong_owner_cannot_refresh_another_accounts_quote(self):
        result = self.apply()
        with self.assertRaises(LookupError):
            prices.refresh_resolution(self.db, -1, self.resolution_id(result), lookup=Lookup())

    def test_refresh_endpoint_scopes_to_signed_in_owner_and_rejects_foreign_origin(self):
        from tests.test_reconciliation_database import TestReconciliationDatabase
        self.apply()
        client = TestReconciliationDatabase.client(self, self.user_id)
        with patch('app.account_pricing.YahooListingLookup', return_value=Lookup()):
            response = client.post('/imports/pricing/refresh')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['fetched'], 1)
        self.assertEqual(self.price(), 20)
        self.assertEqual(client.post('/imports/pricing/refresh', headers={'origin': 'https://evil.invalid'}).status_code, 403)
        self.assertEqual(TestReconciliationDatabase.client(self).post('/imports/pricing/refresh').status_code, 401)

    def test_observation_age_and_provider_identity_checked_and_retry_preserves_recent_quote(self):
        result = self.apply()
        self.refresh(result, lookup=Lookup())
        for changes in ({'isin': 'US0000000001'}, {'observed_at': (dt.datetime.utcnow() - dt.timedelta(days=8)).isoformat()}):
            item = self.refresh(result, lookup=Lookup(**changes), force=True)
            self.assertEqual(item['status'], 'unavailable')
            self.assertEqual(self.price(), 20)  # retained verified quote still current

    def test_holding_detail_uses_scoped_quote(self):
        from tests.test_reconciliation_database import TestReconciliationDatabase
        result = self.apply()
        self.refresh(result, lookup=Lookup())
        client = TestReconciliationDatabase.client(self, self.user_id)
        response = client.get(f'/holding/{self.ticker}?account=platform:{self.platform_id}')
        self.assertEqual(response.status_code, 200, response.text[:500])

    def test_new_statement_invalidates_previous_quote_until_refreshed_and_undo_removes_cache(self):
        from app import scoped_identity
        def statement(date, quantity):
            raw = f'Symbol,Qty,Price,Average Price,ISIN,MIC\n{self.ticker},{quantity},£15,£12,GB00BP6MXD84,XLON\n'.encode()
            item = ingestion.stage(self.db, self.user_id, self.platform_id, 'ii_holdings', raw,
                                   'statement.csv', effective_date=date)
            ingestion.apply(self.db, self.user_id, item.id)
        statement('2026-10-30', 7)
        result = self.apply(self.review(namespace='holdings:ii_holdings'))
        resolution_id = self.resolution_id(result)
        self.refresh(result, lookup=Lookup())
        self.assertEqual(self.price(), 20)
        statement('2026-10-31', 8)
        self.assertIsNone(self.price())
        self.refresh(result, lookup=Lookup())
        self.assertEqual(self.price(), 20)
        scoped_identity.undo(self.db, self.user_id, result['audit_id'])
        self.assertIsNone(self.db.get(AccountListingPrice, resolution_id))
        self.assertEqual(self.price(), 15)

    def test_read_only_comparison_detects_changed_and_unavailable_prices(self):
        from scripts.compare_account_prices import compare
        result = self.apply()
        self.assertEqual(compare(self.db, self.user_id)['newly_unavailable'], 1)
        self.refresh(result, lookup=Lookup())
        self.assertEqual(compare(self.db, self.user_id)['changed'], 1)

    def test_snapshot_freezes_verified_quote_with_missing_catalogue_currency(self):
        from app.snapshots import snapshot_csv_platforms
        from app.models import HoldingSnapshot
        self.instrument.currency_code = None
        self.db.commit()
        result = self.apply()
        self.refresh(result, lookup=Lookup())
        snapshot_csv_platforms(self.db)
        saved = self.db.query(HoldingSnapshot).filter_by(user_id=self.user_id).one()
        self.assertEqual(saved.price_currency, 'GBP')
        self.assertEqual(saved.price_native, 20)
        self.assertEqual(saved.value_gbp, 140)
        self.assertEqual(saved.fx_rate_to_gbp, 1)
        self.assertEqual(saved.valuation_evidence['quote_currency'], 'GBX')
        self.assertEqual(saved.valuation_evidence['listing_id'], result['listing_id'])
        self.assertIsNone(self.instrument.currency_code)
