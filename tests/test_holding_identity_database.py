import datetime as dt
import json
import unittest
from unittest.mock import patch
from sqlalchemy import text
from starlette.requests import Request
from app import listing_identity, scoped_identity
from app.holding_identity import holding_scope
from app.models import Instrument, Position, Platform, Order, DividendPayment, HoldingSnapshot
from tests import test_import_database as fixtures
from tests import test_scoped_identity_database as scoped_tests
from tests.test_listing_identity_database import Lookup


@unittest.skipUnless(fixtures.os.environ.get('TEST_DATABASE_URL'), 'requires disposable PostgreSQL')
class TestHoldingIdentity(unittest.TestCase):
    setUpClass = classmethod(fixtures.TestImportDatabase.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.TestImportDatabase.tearDownClass.__func__)
    cleanup_records = fixtures.TestImportDatabase.cleanup_records
    setUp = scoped_tests.TestScopedIdentityDatabase.setUp

    def prepare(self, verified=True):
        self.platform.platform_type = 't212_api'
        self.instrument.yf_ticker = self.symbol
        self.instrument.exchange = 'LSE'
        original = self.db.get(Position, (self.ticker, self.platform_id, self.user_id))
        original.cost_basis_amount, original.cost_basis_currency, original.cost_basis_status = 14, 'GBP', 'reported'
        self.other = Platform(user_id=self.user_id, name='II GIA', platform_type='interactive_investor', account_type='GIA', currency='GBP')
        self.db.add(self.other); self.db.flush()
        self.alias = self.ticker + '_alias'
        self.extra_instruments = [self.alias]
        self.db.add(Instrument(ticker=self.alias, isin=self.instrument.isin, mic_code='XLON',
            currency_code='GBX', yf_ticker=self.symbol, exchange='LSE'))
        self.db.flush()
        self.db.add(Position(user_id=self.user_id, platform_id=self.other.id, ticker=self.alias,
            quantity=3, current_price=2000, cost_basis_amount=60, cost_basis_currency='GBP', cost_basis_status='reported'))
        for index, (symbol,pid) in enumerate([(self.ticker,self.platform_id),(self.alias,self.other.id)]):
            self.db.add(Order(user_id=self.user_id, platform_id=pid, ticker=symbol, id='same-broker-id', status='FILLED', quantity=1, side='BUY'))
            self.db.add(DividendPayment(user_id=self.user_id, platform_id=pid, ticker=symbol, reference='same-reference', amount=index+1))
            self.db.add(HoldingSnapshot(user_id=self.user_id, platform_id=pid, ticker=symbol, snapshot_date=dt.date(2026,11,1), quantity=7 if index==0 else 3, price_native=13 if index==0 else 2000, value_gbp=91 if index==0 else 60))
        self.db.commit()
        if verified:
            for symbol in [self.ticker,self.alias]:
                review=listing_identity.verify(self.db,self.user_id,symbol,self.symbol,lookup=Lookup())
                self.assertTrue(review['can_apply'])
                listing_identity.apply(self.db,self.user_id,review['review_token'])
        def cleanup_links():
            self.db.rollback()
            self.db.execute(text('DELETE FROM instrument_listing_links WHERE ticker=ANY(:symbols)'), {'symbols':[self.ticker,self.alias]})
            self.db.commit()
        self.addCleanup(cleanup_links)

    def page(self, symbol=None, account='portfolio'):
        from app.web.server import holding_detail
        request=Request({'type':'http','method':'GET','path':'/holding/'+(symbol or self.ticker),
            'headers':[], 'query_string':b'', 'session':{'user_id':self.user_id}})
        with patch('app.web.server._get_fx_rates_to_gbp',return_value={'GBP':1,'GBX':.01,'USD':.8}):
            return holding_detail(symbol or self.ticker,request,account=account,db=self.db)

    def records(self):
        return {table:self.db.execute(text(f'SELECT row_to_json(r)::text FROM {table} r WHERE user_id=:u ORDER BY ticker,platform_id'),{'u':self.user_id}).scalars().all()
                for table in ['positions','orders','dividend_payments','holding_snapshots']}

    def test_both_alias_urls_combine_accounts_without_changing_original_records(self):
        self.prepare()
        before=self.records()
        for symbol in [self.ticker,self.alias]:
            response=self.page(symbol)
            self.assertEqual(response.status_code,200)
            ctx=response.context
            self.assertEqual(ctx['total_quantity'],10)
            self.assertAlmostEqual(ctx['value_display'],151)
            self.assertAlmostEqual(ctx['cost_display'],74)
            self.assertEqual(len(ctx['all_positions']),2)
            self.assertEqual(len(ctx['trade_history']),2)
            self.assertEqual(len(ctx['div_payments']),2)
            self.assertEqual(ctx['total_divs_display'],3)
            self.assertEqual(json.loads(ctx['holding_history'])['values'],[151])
            self.assertIn(b'T212-ISA',response.body)
            self.assertIn(b'II-GIA',response.body)
        self.assertEqual(self.records(),before)

    def test_portfolio_and_analysis_group_verified_aliases_after_conversion(self):
        from app.web import server

        self.prepare()
        request = Request({
            'type': 'http', 'method': 'GET', 'path': '/portfolio', 'headers': [],
            'query_string': b'', 'session': {'user_id': self.user_id},
        })
        with patch('app.web.server._get_fx_rates_to_gbp', return_value={'GBP': 1, 'GBX': .01}):
            portfolio = server.index(request, db=self.db).context
            analysis = server.portfolio_analysis(request, db=self.db).context
            filtered = server.portfolio_analysis(
                request, account=f'platform_{self.other.id}', db=self.db,
            ).context
        for rows in (portfolio['holdings'], analysis['positions']):
            self.assertEqual(len(rows), 1)
            self.assertAlmostEqual(rows[0]['value_display'], 151)
            self.assertEqual({badge['id'] for badge in rows[0]['holding_accounts']},
                             {self.platform_id, self.other.id})
        self.assertAlmostEqual(analysis['positions'][0]['cost_display'], 74)
        self.assertAlmostEqual(filtered['positions'][0]['value_display'], 60)

    def test_selected_account_filters_all_alias_queries(self):
        self.prepare()
        response=self.page(account=f'platform_{self.other.id}')
        ctx=response.context
        self.assertEqual(ctx['total_quantity'],3)
        self.assertAlmostEqual(ctx['value_display'],60)
        self.assertEqual({o.platform_id for o in ctx['trade_history']},{self.other.id})
        self.assertEqual({p.platform_id for p in ctx['div_payments']},{self.other.id})
        self.assertEqual(json.loads(ctx['holding_history'])['values'],[60])

    def test_isin_and_symbol_hints_without_verification_do_not_group(self):
        self.prepare(verified=False)
        self.assertEqual(holding_scope(self.db,self.user_id,self.ticker)[0],[])
        self.assertEqual(self.page().context['total_quantity'],7)

    def test_verified_provider_reference_can_validate_equivalent_broker_alias(self):
        self.prepare()
        self.db.execute(text('DELETE FROM instrument_listing_links WHERE ticker=:ticker'), {'ticker':self.alias})
        self.db.commit()
        self.assertIn((self.alias,self.other.id),holding_scope(self.db,self.user_id,self.ticker)[0])

    def test_stale_global_link_does_not_fall_back_to_a_symbol_hint(self):
        self.prepare()
        self.db.get(Instrument,self.alias).exchange='changed'
        self.db.commit()
        self.assertNotIn((self.alias,self.other.id),holding_scope(self.db,self.user_id,self.ticker)[0])

    def test_other_share_class_is_not_grouped_even_with_same_symbol_hint(self):
        self.prepare()
        self.db.get(Instrument,self.alias).isin='US0000000001'
        self.db.commit()
        self.assertNotIn((self.alias,self.other.id),holding_scope(self.db,self.user_id,self.ticker)[0])
        self.assertEqual(self.page().context['total_quantity'],7)

    def test_stale_scoped_link_blocks_global_fallback(self):
        self.prepare()
        review=scoped_identity.verify(self.db,self.user_id,self.other.id,'holdings:legacy',self.alias,
            self.symbol,isin=self.instrument.isin,currency='GBX',mic='XLON',statement_reference='Test statement',lookup=Lookup())
        scoped_identity.apply(self.db,self.user_id,review['review_token'])
        self.db.get(Instrument,self.alias).exchange='changed'
        self.db.commit()
        self.assertNotIn((self.alias,self.other.id),holding_scope(self.db,self.user_id,self.ticker)[0])

    def test_removed_scoped_link_does_not_resurrect_global_link(self):
        self.prepare()
        review=scoped_identity.verify(self.db,self.user_id,self.other.id,'holdings:legacy',self.alias,
            self.symbol,isin=self.instrument.isin,currency='GBX',mic='XLON',statement_reference='Test statement',lookup=Lookup())
        applied=scoped_identity.apply(self.db,self.user_id,review['review_token'])
        scoped_identity.undo(self.db,self.user_id,applied['audit_id'])
        self.assertNotIn((self.alias,self.other.id),holding_scope(self.db,self.user_id,self.ticker)[0])

    def test_sold_alias_keeps_its_events_and_historical_value(self):
        self.prepare()
        self.db.delete(self.db.get(Position,(self.alias,self.other.id,self.user_id)))
        self.db.commit()
        ctx=self.page().context
        self.assertEqual(ctx['total_quantity'],7)
        self.assertEqual(len(ctx['trade_history']),2)
        self.assertEqual(len(ctx['div_payments']),2)
        self.assertEqual(json.loads(ctx['holding_history'])['values'],[151])

    def test_alias_discovery_is_owner_scoped(self):
        self.prepare()
        self.assertEqual(holding_scope(self.db,-1,self.ticker)[0],[])

    def test_foreign_or_empty_account_never_falls_back_to_all_positions(self):
        self.prepare()
        ctx=self.page(account='platform_2147483647').context
        self.assertEqual(ctx['total_quantity'],0)
        self.assertEqual(ctx['value_display'],0)
        self.assertEqual(ctx['trade_history'],[])
        self.assertEqual(ctx['div_payments'],[])

    def test_verified_different_listing_currency_is_converted_before_combining(self):
        self.prepare()
        instrument=self.db.get(Instrument,self.alias)
        instrument.currency_code, instrument.mic_code, instrument.exchange, instrument.yf_ticker = 'USD','XNYS','NYSE',self.symbol+'US'
        self.db.get(Position,(self.alias,self.other.id,self.user_id)).current_price=100
        self.db.commit()
        review=listing_identity.verify(self.db,self.user_id,self.alias,instrument.yf_ticker,
            lookup=Lookup(currency='USD',mic='XNYS',exchange='NYSE',price='100'))
        listing_identity.apply(self.db,self.user_id,review['review_token'])
        for symbol in [self.ticker,self.alias]:
            ctx=self.page(symbol).context
            self.assertEqual(ctx['total_quantity'],10)
            self.assertAlmostEqual(ctx['value_display'],331)

    def test_retained_execution_units_conflict_blocks_alias_grouping(self):
        self.prepare()
        order=self.db.query(Order).filter_by(user_id=self.user_id,ticker=self.alias).one()
        order.execution_currency='USD'
        self.db.commit()
        self.assertNotIn((self.alias,self.other.id),holding_scope(self.db,self.user_id,self.ticker)[0])
