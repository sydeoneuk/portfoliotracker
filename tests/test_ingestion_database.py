"""Real PostgreSQL staging, retained evidence, authority and isolation tests."""
import datetime
import io
import unittest
from unittest.mock import patch
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from app import ingestion
from app.account_lock import account_write_lock
from app.models import Position, Order, Transaction, IngestionBatch, IngestionRow, ImportHoldingObservation, ImportAuthority
from tests import test_import_database as fixtures
from tests import test_reconciliation_database as web_fixtures


@unittest.skipUnless(fixtures.os.environ.get('TEST_DATABASE_URL'), 'requires disposable PostgreSQL')
class TestIngestionDatabase(unittest.TestCase):
    setUpClass = classmethod(fixtures.TestImportDatabase.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.TestImportDatabase.tearDownClass.__func__)
    cleanup_records = fixtures.TestImportDatabase.cleanup_records
    client = web_fixtures.TestReconciliationDatabase.client
    trade_csv = fixtures.TestImportDatabase.trade_csv

    def setUp(self):
        fixtures.TestImportDatabase.setUp(self)
        self.db.add(Position(user_id=self.user_id, platform_id=self.platform_id, ticker=self.ticker,
            quantity=7, average_price=12, current_price=13))
        self.db.commit()

    def content(self, quantity=10):
        return f'Symbol,Name,Qty,Price,Average Price\n{self.ticker},Test,{quantity},£15,£12\n'.encode()

    def stage(self, content=None, **kwargs):
        return ingestion.stage(self.db, self.user_id, self.platform_id, kwargs.pop('source', 'ii_holdings'),
            content if content is not None else self.content(), 'statement.csv',
            effective_date=kwargs.pop('effective_date', '2026-10-30'), **kwargs)

    def position(self):
        self.db.expire_all()
        return self.db.get(Position, (self.ticker, self.platform_id, self.user_id))

    def test_preview_retains_file_rows_observations_without_changing_current_holdings(self):
        raw = self.content()
        batch = self.stage(raw)
        self.assertEqual(batch.status, 'review')
        self.assertEqual(self.position().quantity, 7)
        self.assertEqual(batch.raw_content, raw)
        self.assertEqual(self.db.query(IngestionRow).filter_by(batch_id=batch.id).count(), 2)
        self.assertEqual(self.db.query(ImportHoldingObservation).filter_by(batch_id=batch.id).count(), 2)
        self.assertEqual(batch.review['current_authority']['source'], 'legacy/unknown')
        self.assertEqual(batch.review['proposed'][0]['quantity'], 10)
        self.assertEqual(batch.review['before'][0]['quantity'], 7)
        effect = ingestion.describe(batch)['import_effect']
        self.assertEqual(effect['holdings_count'], 1)
        self.assertEqual(effect['costs_unavailable'], 0)
        self.assertIn('absent from the statement', effect['explanation'])
        self.assertEqual(self.db.query(ImportAuthority).filter_by(platform_id=self.platform_id).count(), 0)

    def test_ii_foreign_quote_and_account_currency_cost_apply_without_converting_cost_twice(self):
        from app.models import Instrument
        self.db.get(Instrument, self.ticker).currency_code = 'USD'
        self.db.commit()
        for average, book, expected in [('£0.00', '£0.00', None), ('£5', '£49', 49)]:
            with self.subTest(book=book):
                raw = f'Symbol,Qty,Price,Average Price,Book Cost\n{self.ticker},10,$6.467,{average},{book}\n'.encode()
                batch = self.stage(raw, effective_date='2026-10-30' if expected is None else '2026-10-31')
                self.assertEqual(batch.status, 'review', batch.review)
                self.assertEqual(ingestion.describe(batch)['import_effect']['costs_unavailable'], int(expected is None))
                ingestion.apply(self.db, self.user_id, batch.id)
                position = self.position()
                self.assertEqual(position.current_price, 6.467)
                self.assertIsNone(position.average_price)
                self.assertEqual(position.cost_basis_currency, 'GBP')
                self.assertEqual(position.cost_basis_amount, expected)

    def test_ii_totals_footer_and_pence_quotes_preserve_recorded_pound_units(self):
        raw = f'Symbol,Name,Qty,Price,Average Price,Book Cost\n{self.ticker},Test,10,646.7p,£5,£49\n,,,Totals,,\n,,,GBP,,£49\n'.encode()
        batch = self.stage(raw)
        self.assertEqual(batch.status, 'review', batch.review)
        self.assertEqual(batch.review['skipped'], 2)
        ingestion.apply(self.db, self.user_id, batch.id)
        position = self.position()
        self.assertAlmostEqual(position.current_price, 6.467)
        self.assertEqual(position.average_price, 5)
        self.assertEqual(position.cost_basis_amount, 49)
        self.assertEqual(position.cost_basis_currency, 'GBP')

    def test_apply_is_atomic_and_visible_from_another_session_and_retry_is_idempotent(self):
        batch = self.stage()
        result = ingestion.apply(self.db, self.user_id, batch.id)
        self.assertEqual(result.status, 'applied')
        with Session(self.engine) as reader:
            self.assertEqual(reader.get(Position, (self.ticker, self.platform_id, self.user_id)).quantity, 10)
            self.assertEqual(reader.get(ImportAuthority, (self.platform_id, 'account')).batch_id, batch.id)
        self.assertEqual(ingestion.apply(self.db, self.user_id, batch.id).status, 'applied')

    def test_invalid_file_and_database_failure_preserve_data_and_retain_rejection(self):
        raw = self.content() + b'BAD,Bad,not-a-number,invalid,1\n'
        rejected = self.stage(raw)
        self.assertEqual(rejected.status, 'rejected')
        self.assertEqual(rejected.raw_content, raw)
        self.assertEqual(self.position().quantity, 7)
        self.assertEqual(self.db.query(IngestionRow).filter_by(batch_id=rejected.id).count(), 3)
        def fail(db, batch):
            db.execute(text('UPDATE positions SET quantity=999 WHERE user_id=:u'), {'u': self.user_id})
            db.execute(text('SELECT 1/0'))
        with patch.object(ingestion, 'run_adapter', side_effect=fail):
            rejected = self.stage()
        self.assertEqual(rejected.status, 'rejected')
        self.assertEqual(self.position().quantity, 7)

    def test_older_and_conflicting_same_date_snapshots_are_retained_but_not_applied(self):
        selected = self.stage()
        ingestion.apply(self.db, self.user_id, selected.id)
        for date in ('2026-10-29', '2026-10-30'):
            rejected = self.stage(self.content(22), effective_date=date)
            self.assertEqual(rejected.status, 'rejected')
            with self.assertRaises(ValueError):
                ingestion.apply(self.db, self.user_id, rejected.id)
            self.db.rollback()
        self.assertEqual(self.position().quantity, 10)

    def test_pending_preview_cannot_overwrite_newer_account_state(self):
        batch = self.stage()
        self.position().quantity = 88
        self.db.commit()
        with self.assertRaisesRegex(ValueError, 'Account changed'):
            ingestion.apply(self.db, self.user_id, batch.id)
        self.db.rollback()
        self.assertEqual(self.position().quantity, 88)

    def test_partial_trade_history_imports_events_without_replacing_holdings(self):
        self.platform.platform_type = 't212_csv'
        self.db.commit()
        batch = self.stage(self.trade_csv([('Market buy', 2, 10, 20, 0)]).getvalue().encode(),
            source='t212_history', effective_date=None)
        self.assertEqual(batch.status, 'review', batch.review)
        ingestion.apply(self.db, self.user_id, batch.id)
        self.assertEqual(self.position().quantity, 7)
        self.assertEqual(self.db.query(Order).filter_by(user_id=self.user_id).count(), 1)
        self.assertEqual(self.db.query(ImportAuthority).filter_by(user_id=self.user_id).count(), 0)

    def test_explicit_complete_trade_history_changes_authority_only_after_review(self):
        self.platform.platform_type = 't212_csv'
        self.db.commit()
        batch = self.stage(self.trade_csv([('Market buy', 2, 10, 20, 0)]).getvalue().encode(),
            source='t212_history', effective_date=None, history_complete=True,
            coverage_start='2026-01-01', coverage_end='2026-01-31')
        self.assertEqual(batch.status, 'review', batch.review)
        self.assertEqual(self.position().quantity, 7)
        ingestion.apply(self.db, self.user_id, batch.id)
        self.assertEqual(self.position().quantity, 2)
        self.assertEqual(self.db.get(ImportAuthority, (self.platform_id, 'account')).batch_id, batch.id)

    def test_api_accounts_reject_csv_holdings_authority(self):
        self.platform.platform_type = 't212_api'
        self.db.commit()
        batch = self.stage()
        self.assertEqual(batch.status, 'rejected')
        self.assertEqual(self.position().quantity, 7)

    def test_lock_blocks_overlapping_account_writes_and_releases_on_failure(self):
        with account_write_lock(self.db, self.platform_id):
            with self.assertRaisesRegex(ValueError, 'being updated'):
                self.stage()
        self.assertEqual(self.stage().status, 'review')

    def test_http_upload_review_apply_and_isolation(self):
        client = self.client(self.user_id)
        response = client.post('/import/ii-csv', data={'platform_id': str(self.platform_id), 'effective_date': '2026-10-30'},
            files={'csv_file': ('statement.csv', self.content(), 'text/csv')})
        self.assertEqual(response.status_code, 200, response.text)
        batch_id = response.json()['batch_id']
        self.assertEqual(self.position().quantity, 7)
        self.assertEqual(client.get('/imports').json()['items'][0]['batch_id'], batch_id)
        self.assertEqual(client.get('/imports/' + str(batch_id)).status_code, 200)
        self.assertEqual(client.post('/imports/apply', json={'batch_id': batch_id, 'reviewed': False}).status_code, 400)
        self.assertEqual(client.post('/imports/apply', json={'batch_id': batch_id, 'reviewed': True}).status_code, 200)
        self.assertEqual(self.position().quantity, 10)
        self.assertEqual(self.client().get('/imports').status_code, 401)
        with self.assertRaises(LookupError):
            ingestion.apply(self.db, self.user_id + 999999, batch_id)
        self.assertEqual(client.post('/import/pie-accept', data={'platform_id': self.platform_id}).status_code, 409)

    def test_effective_dates_required_and_future_dates_are_rejected(self):
        for date in (None, '2100-01-01'):
            with self.assertRaises(ValueError):
                self.stage(effective_date=date)
        self.assertEqual(self.position().quantity, 7)

    def test_ii_transactions_stage_without_mutating_and_persist_when_applied(self):
        raw = b'Date,Symbol,Description,Reference,Debit,Credit\n25/10/2026,n/a,GROSS INTEREST,reference,n/a,1.05\n'
        batch = self.stage(raw, source='ii_transactions', effective_date=None)
        self.assertEqual(batch.status, 'review', batch.review)
        self.assertEqual(self.db.query(Transaction).filter_by(user_id=self.user_id).count(), 0)
        ingestion.apply(self.db, self.user_id, batch.id)
        self.assertEqual(self.db.query(Transaction).filter_by(user_id=self.user_id).count(), 1)

    def test_pie_snapshot_retains_allocation_observation_without_overwriting_account_holdings(self):
        raw = f'Ticker,Name,Shares,Target Allocation (%)\n{self.ticker},Test,2,100\n'.encode()
        batch = self.stage(raw, source='pie_snapshot', pie_name='Retained pie')
        self.assertEqual(batch.status, 'review', batch.review)
        self.assertEqual(self.position().quantity, 7)
        ingestion.apply(self.db, self.user_id, batch.id)
        self.assertEqual(self.position().quantity, 7)
        self.assertIsNone(self.db.get(ImportAuthority, (self.platform_id, 'account')))
        self.assertEqual(self.db.get(ImportAuthority, (self.platform_id, 'pie:Retained pie')).batch_id, batch.id)
        original = self.db.query(ImportHoldingObservation).filter_by(batch_id=batch.id, phase='applied').one()
        self.assertFalse(original.complete)
        self.assertEqual(original.holdings[0]['quantity'], 2)

    def test_apply_failure_rolls_back_changes_but_retains_original_preview(self):
        batch = self.stage()
        batch_id = batch.id
        def fail(db, batch):
            db.execute(text('UPDATE positions SET quantity=999 WHERE user_id=:u'), {'u': self.user_id})
            raise ValueError('simulated application failure')
        with patch.object(ingestion, 'run_adapter', side_effect=fail):
            with self.assertRaisesRegex(ValueError, 'simulated'):
                ingestion.apply(self.db, self.user_id, batch_id)
        self.assertEqual(self.position().quantity, 7)
        retained = self.db.get(IngestionBatch, batch_id)
        self.assertEqual(retained.status, 'review')
        self.assertEqual(retained.raw_content, self.content())
        self.assertIsNone(retained.applied_at)

    def test_original_projection_fields_and_timestamps_survive_explicit_replacement(self):
        position = self.position()
        position.ppl, position.fx_ppl = 123, 4
        self.db.commit()
        original_time = position.last_synced_at
        batch = self.stage()
        ingestion.apply(self.db, self.user_id, batch.id)
        original = self.db.query(ImportHoldingObservation).filter_by(batch_id=batch.id, phase='before').one()
        self.assertEqual(original.holdings[0]['ppl'], 123)
        self.assertEqual(original.holdings[0]['fx_ppl'], 4)
        self.assertEqual(original.holdings[0]['last_synced_at'], str(original_time))

    def test_source_row_outside_complete_history_coverage_cannot_be_selected(self):
        self.platform.platform_type = 't212_csv'
        self.db.commit()
        batch = self.stage(self.trade_csv([('Market buy', 2, 10, 20, 0)]).getvalue().encode(),
            source='t212_history', effective_date=None, history_complete=True,
            coverage_start='2026-02-01', coverage_end='2026-02-28')
        self.assertEqual(batch.status, 'rejected')
        self.assertEqual(self.position().quantity, 7)

    def test_repeated_file_is_identified_without_duplicating_holdings(self):
        first = self.stage()
        ingestion.apply(self.db, self.user_id, first.id)
        repeated = self.stage()
        self.assertEqual(repeated.review['exact_file_seen_in_batch'], first.id)
        ingestion.apply(self.db, self.user_id, repeated.id)
        self.assertEqual(self.position().quantity, 10)
        self.assertEqual(self.db.query(Position).filter_by(user_id=self.user_id).count(), 1)

    def test_legacy_nonfinite_values_are_retained_as_evidence_not_invalid_json(self):
        self.position().ppl = float('nan')
        self.db.commit()
        batch = self.stage()
        self.assertEqual(batch.status, 'review', batch.review)
        original = self.db.query(ImportHoldingObservation).filter_by(batch_id=batch.id, phase='before').one()
        self.assertEqual(original.holdings[0]['ppl'], 'nan')

    def test_legacy_sync_script_passes_an_owned_platform_instead_of_obsolete_account_keyword(self):
        from scripts import sync
        self.platform.platform_type, self.platform.account_type = 't212_api', 'GIA'
        self.db.commit()
        with patch.object(sync, 'SessionLocal', return_value=self.db), \
             patch.object(sync, 'SyncRunner') as runner, \
             patch.object(sync.settings, 't212_api_key', 'disposable-test-key'), \
             patch.object(sync.settings, 't212_isa_api_key', None), \
             patch('sys.argv', ['sync.py', '--user-id', str(self.user_id)]):
            sync.main()
        self.assertEqual(runner.call_args.kwargs['platform'].id, self.platform_id)
        self.assertNotIn('account', runner.call_args.kwargs)
        runner.return_value.sync_all.assert_called_once()

    def test_explicit_platform_deletion_removes_its_retained_children_without_fk_errors(self):
        batch = self.stage()
        ingestion.apply(self.db, self.user_id, batch.id)
        response = self.client(self.user_id).post(f'/settings/platforms/{self.platform_id}/delete')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.db.query(IngestionBatch).filter_by(user_id=self.user_id).count(), 0)

    def test_clearing_projections_preserves_import_evidence_but_invalidates_pending_previews(self):
        batch = self.stage()
        response = self.client(self.user_id).post('/settings/clear-data', follow_redirects=False)
        self.assertEqual(response.status_code, 303, response.text)
        self.db.expire_all()
        self.assertEqual(self.db.get(IngestionBatch, batch.id).status, 'superseded')
        self.assertEqual(self.db.get(IngestionBatch, batch.id).raw_content, self.content())
