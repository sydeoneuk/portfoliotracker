import datetime as dt
import json
import unittest
from unittest.mock import MagicMock
from sqlalchemy import select
from app.models import Transaction, IngestionBatch, Platform
from app.sync.runner import SyncRunner
from app.sync.legacy_transactions import sync_notice
from tests import test_import_database as fixtures


@unittest.skipUnless(fixtures.os.environ.get('TEST_DATABASE_URL'), 'requires disposable PostgreSQL')
class TestLegacyCashSync(unittest.TestCase):
    setUpClass = classmethod(fixtures.TestImportDatabase.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.TestImportDatabase.tearDownClass.__func__)
    cleanup_records = fixtures.TestImportDatabase.cleanup_records

    def setUp(self):
        fixtures.TestImportDatabase.setUp(self)
        self.platform.platform_type = 't212_api'
        self.legacy = Transaction(id='legacy-cash', user_id=self.user_id, platform_id=None,
            source_namespace='legacy', type='DEPOSIT', amount=12.34,
            date_time=dt.datetime(2026, 1, 1, 12), reference='legacy-cash', notes='Keep original notes')
        self.db.add(self.legacy); self.db.commit()
        self.pk = self.legacy.pk
        self.runner = SyncRunner.__new__(SyncRunner)
        self.runner.session, self.runner.user_id = self.db, self.user_id
        self.runner.platform, self.runner.platform_id = self.platform, self.platform_id
        self.runner.client = MagicMock()

    def event(self, **changes):
        return {'reference': 'legacy-cash', 'type': 'DEPOSIT', 'amount': 12.34,
                'dateTime': '2026-01-01T12:00:00Z', 'notes': 'New API note', **changes}

    def run_sync(self, items):
        self.runner.client.get_transactions.return_value = {'items': items}
        self.runner._run_step(self.runner.sync_transactions)
        self.db.expire_all()

    def test_exact_api_match_preserves_pk_financial_fields_and_retries_without_duplicate(self):
        for attempt in range(2):
            self.run_sync([self.event()])
            rows = self.db.query(Transaction).filter_by(user_id=self.user_id).all()
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual((row.pk, row.amount, row.type, row.date_time, row.notes),
                (self.pk, 12.34, 'DEPOSIT', dt.datetime(2026, 1, 1, 12), 'Keep original notes'))
            self.assertEqual((row.platform_id, row.source_namespace), (self.platform_id, 't212'))
        audits = self.db.query(IngestionBatch).filter_by(user_id=self.user_id, source='t212_identity_reconciliation').all()
        self.assertEqual(len(audits), 1)
        evidence = json.loads(audits[0].raw_content)['adoptions'][0]
        self.assertEqual(evidence['before']['platform_id'], None)
        self.assertEqual(evidence['after']['platform_id'], self.platform_id)
        self.assertEqual(evidence['before']['amount'], evidence['after']['amount'])
        self.assertEqual(evidence['api_transaction']['reference'], 'legacy-cash')

    def test_conflicting_or_incomplete_event_is_retained_without_blocking_other_transactions(self):
        for changes in ({'amount': 12.35}, {'type': 'WITHDRAW'}, {'dateTime': '2026-01-01T12:00:01Z'},
                        {'amount': None}, {'type': None}, {'dateTime': None}):
            with self.subTest(changes=changes):
                self.run_sync([self.event(**changes), self.event(reference='unrelated', amount=5)])
                row = self.db.get(Transaction, self.pk)
                self.assertIsNone(row.platform_id)
                self.assertEqual(row.amount, 12.34)
                self.assertEqual(self.runner.transaction_identity_summary['deferred'], 1)
                self.assertEqual(self.db.query(Transaction).filter_by(user_id=self.user_id).count(), 2)
                self.assertIn('retained without duplication', sync_notice(self.db, self.user_id, 0))

    def test_full_history_is_replayed_while_legacy_records_remain(self):
        self.db.add(Transaction(id='future', user_id=self.user_id, platform_id=self.platform_id,
            source_namespace='t212', date_time=dt.datetime(2099, 1, 1)))
        self.db.commit()
        self.run_sync([self.event()])
        self.assertIsNone(self.runner.client.get_transactions.call_args.kwargs['newer_than'])

    def test_later_error_rolls_back_assignment_and_evidence(self):
        with self.assertRaises(ValueError):
            self.run_sync([self.event(), {'amount': 9}])
        self.db.expire_all()
        self.assertIsNone(self.db.get(Transaction, self.pk).platform_id)
        self.assertEqual(self.db.query(IngestionBatch).filter_by(user_id=self.user_id).count(), 0)

    def test_equivalent_utc_instant_matches_without_rewriting_original_timestamp(self):
        self.run_sync([self.event(dateTime='2026-01-01T13:00:00+01:00')])
        self.assertEqual(self.db.get(Transaction, self.pk).date_time, dt.datetime(2026, 1, 1, 12))
        self.assertEqual(self.db.get(Transaction, self.pk).platform_id, self.platform_id)

    def test_legacy_deposit_can_match_broker_interest_without_changing_original_type(self):
        self.run_sync([self.event(type='INTEREST_ON_FREE_CASH')])
        row = self.db.get(Transaction, self.pk)
        self.assertEqual((row.type, row.amount, row.platform_id), ('DEPOSIT', 12.34, self.platform_id))
        receipt = self.db.query(IngestionBatch).filter_by(user_id=self.user_id).one()
        entry = json.loads(receipt.raw_content)['adoptions'][0]
        self.assertEqual(entry['type_match'], 'legacy_deposit_to_broker_interest')
        self.assertEqual(entry['api_transaction']['type'], 'INTEREST_ON_FREE_CASH')
        self.assertEqual(entry['before']['type'], entry['after']['type'])

    def test_other_cash_type_changes_and_negative_deposit_interest_are_not_assumed_equivalent(self):
        self.run_sync([self.event(type='DIVIDEND')])
        self.assertIsNone(self.db.get(Transaction, self.pk).platform_id)
        self.legacy.amount = -12.34
        self.db.commit()
        self.run_sync([self.event(type='INTEREST_ON_FREE_CASH', amount=-12.34)])
        self.assertIsNone(self.db.get(Transaction, self.pk).platform_id)

    def test_existing_scoped_copy_is_not_duplicated_or_used_to_delete_legacy_row(self):
        self.db.add(Transaction(id='legacy-cash', user_id=self.user_id, platform_id=self.platform_id,
            source_namespace='t212', type='DEPOSIT', amount=12.34, date_time=dt.datetime(2026, 1, 1, 12)))
        self.db.commit()
        self.run_sync([self.event()])
        self.assertIsNone(self.db.get(Transaction, self.pk).platform_id)
        self.assertEqual(self.db.query(Transaction).filter_by(user_id=self.user_id).count(), 2)
        self.assertEqual(self.runner.transaction_identity_summary['deferred'], 1)

    def test_two_matching_legacy_rows_require_review(self):
        self.db.add(Transaction(id='legacy-cash', user_id=self.user_id, platform_id=self.platform_id,
            source_namespace='legacy', type='DEPOSIT', amount=12.34, date_time=dt.datetime(2026, 1, 1, 12)))
        self.db.commit()
        self.run_sync([self.event()])
        self.assertEqual(self.db.query(Transaction).filter_by(user_id=self.user_id, source_namespace='legacy').count(), 2)
        self.assertEqual(self.db.query(Transaction).filter_by(user_id=self.user_id).count(), 2)

    def test_wrong_provider_cannot_claim_legacy_cash_and_csv_guard_stays_strict(self):
        self.platform.platform_type = 'interactive_investor'
        self.db.commit()
        self.run_sync([self.event()])
        self.assertIsNone(self.db.get(Transaction, self.pk).platform_id)
        from app.identity import require_resolved_identity, UnresolvedIdentityError
        with self.assertRaises(UnresolvedIdentityError):
            require_resolved_identity(self.db, Transaction, self.user_id, 'id', 'legacy-cash', self.platform_id)

    def test_known_account_unknown_source_only_updates_namespace(self):
        self.legacy.platform_id = self.platform_id
        self.db.commit()
        self.run_sync([self.event()])
        row = self.db.get(Transaction, self.pk)
        self.assertEqual((row.platform_id, row.source_namespace, row.amount), (self.platform_id, 't212', 12.34))

    def test_another_accounts_legacy_event_is_not_moved(self):
        other = Platform(user_id=self.user_id, name='Other', platform_type='t212_api', account_type='GIA', currency='GBP')
        self.db.add(other); self.db.flush()
        self.legacy.platform_id = other.id
        self.db.commit()
        self.run_sync([self.event()])
        self.assertEqual(self.db.get(Transaction, self.pk).platform_id, other.id)
        self.assertEqual(self.db.query(Transaction).filter_by(user_id=self.user_id).count(), 2)

    def test_another_users_legacy_event_is_never_claimed(self):
        from app.auth.models import User
        from sqlalchemy import delete
        other = User(email=self.ticker + '-other@example.invalid', provider='test', provider_id=self.ticker + '-other')
        self.db.add(other); self.db.flush()
        other_id = other.id
        self.legacy.user_id = other_id
        self.db.commit()
        def cleanup_other():
            self.db.rollback()
            self.db.execute(delete(Transaction).where(Transaction.user_id == other_id))
            self.db.execute(delete(User).where(User.id == other_id))
            self.db.commit()
        self.addCleanup(cleanup_other)
        self.run_sync([self.event()])
        row = self.db.get(Transaction, self.pk)
        self.assertEqual(row.user_id, other_id)
        self.assertIsNone(row.platform_id)
        self.assertEqual(self.db.query(Transaction).filter_by(user_id=self.user_id).count(), 1)
