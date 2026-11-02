import json
import unittest
from unittest.mock import patch
from sqlalchemy import text
from app.models import Platform, Position, Instrument, Pie, PieHolding
from app.valuation import execute_position_query
from tests import test_import_database as fixtures
from tests import test_scoped_identity_database as scoped_tests
from tests import test_reconciliation_database as web_tests
from tests.test_listing_identity_database import Lookup


@unittest.skipUnless(fixtures.os.environ.get('TEST_DATABASE_URL'), 'requires disposable PostgreSQL')
class TestAnalysisQueries(unittest.TestCase):
    setUpClass = classmethod(fixtures.TestImportDatabase.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.TestImportDatabase.tearDownClass.__func__)
    cleanup_records = fixtures.TestImportDatabase.cleanup_records
    setUp = scoped_tests.TestScopedIdentityDatabase.setUp
    review = scoped_tests.TestScopedIdentityDatabase.review
    apply = scoped_tests.TestScopedIdentityDatabase.apply
    client = web_tests.TestReconciliationDatabase.client

    def prepare(self):
        from app.account_pricing import refresh_resolution
        from app.models import AccountListingResolution
        self.apply()
        row = self.db.query(AccountListingResolution).filter_by(user_id=self.user_id).one()
        refresh_resolution(self.db, self.user_id, row.id, lookup=Lookup())
        self.other = Platform(user_id=self.user_id, name='Other account', platform_type='interactive_investor',
                              account_type='GIA', currency='GBP')
        self.db.add(self.other); self.db.flush()
        self.db.add(Position(user_id=self.user_id, platform_id=self.other.id, ticker=self.ticker,
                             quantity=3, average_price=15, current_price=13))
        missing = self.ticker + '_missing'
        self.extra_instruments = [missing]
        self.db.add(Instrument(ticker=missing, currency_code='GBP'))
        self.db.flush()
        self.db.add(Position(user_id=self.user_id, platform_id=self.platform_id, ticker=missing,
                             quantity=1, current_price=None))
        self.pie = Pie(id=1, user_id=self.user_id, platform_id=self.platform_id, name='Selected pie', account='ISA')
        self.db.add(self.pie); self.db.flush()
        self.db.add(PieHolding(user_id=self.user_id, pie_id=self.pie.pk, ticker=self.ticker, owned_quantity=2))
        self.db.commit()

    def test_all_portfolio_query_shapes_preserve_values_and_missing_price_guards(self):
        from app.web import server
        self.prepare()
        queries = [server._COMBINED_SQL, server._PLATFORM_SQL, server._ACCOUNT_TYPE_SQL,
                   server._build_multi_platform_sql([self.platform_id, self.other.id]),
                   server._pie_query([self.pie.pk], 'portfolio'), server._no_pie_query('portfolio')]
        params = {'user_id': self.user_id, 'platform_id': self.platform_id, 'account_type': 'ISA'}
        canonical = lambda rows: sorted(json.dumps(dict(row._mapping), sort_keys=True, default=str) for row in rows)
        for query in queries:
            with self.subTest(shape=queries.index(query)):
                self.db.execute(text('SET LOCAL jit=off'))
                expected = self.db.execute(text(str(query)), params).fetchall()
                actual = execute_position_query(self.db, query, params)
                self.assertEqual(canonical(actual), canonical(expected))
        combined = execute_position_query(self.db, server._COMBINED_SQL, params)
        unavailable = next(row for row in combined if row.ticker.endswith('_missing'))
        self.assertIsNone(unavailable.value)

    def test_query_restores_prior_jit_setting_without_changing_global_configuration(self):
        from app.web.server import _COMBINED_SQL
        for mode in ('on', 'off'):
            self.db.execute(text('SELECT set_config(\'jit\', :mode, true)'), {'mode': mode})
            execute_position_query(self.db, _COMBINED_SQL, {'user_id': self.user_id})
            self.assertEqual(self.db.execute(text('SHOW jit')).scalar_one(), mode)

    def test_analysis_reuses_whole_portfolio_rows_but_fetches_full_total_for_account_filter(self):
        from app.web import server
        self.prepare()
        client = self.client(self.user_id)
        with patch.object(server, '_get_fx_rates_to_gbp', return_value={'GBP': 1, 'GBX': .01}), \
                patch.object(server, '_fetch_filtered_positions', wraps=server._fetch_filtered_positions) as fetch:
            response = client.get('/analysis')
            self.assertEqual(response.status_code, 200, response.text[:300])
            self.assertEqual(fetch.call_count, 1)
            fetch.reset_mock()
            response = client.get(f'/analysis?account=platform_{self.platform_id}')
            self.assertEqual(response.status_code, 200, response.text[:300])
            self.assertEqual(fetch.call_count, 2)

    def test_reused_portfolio_total_keeps_unfiltered_native_pence_values(self):
        from app.web import server
        self.instrument.currency_code = 'GBX'
        self.instrument.country = 'United Kingdom'
        self.db.get(Position, (self.ticker, self.platform_id, self.user_id)).current_price = 2000
        other = self.ticker + '_other'
        self.extra_instruments = [other]
        self.db.add(Instrument(ticker=other, currency_code='GBP', country='United States'))
        self.db.flush()
        self.db.add(Position(user_id=self.user_id, platform_id=self.platform_id, ticker=other, quantity=1, current_price=60))
        self.db.commit()
        client = self.client(self.user_id)
        with patch.object(server, '_get_fx_rates_to_gbp', return_value={'GBP': 1, 'GBX': .01}), \
                patch.object(server, '_fetch_filtered_positions', wraps=server._fetch_filtered_positions) as fetch:
            response = client.get('/analysis?country=United%20Kingdom')
        self.assertEqual(response.status_code, 200, response.text[:300])
        self.assertEqual(fetch.call_count, 1)
        self.assertAlmostEqual(response.context['analysis_summary']['value'], 140)
        self.assertAlmostEqual(response.context['total_portfolio_pct'], 70)
