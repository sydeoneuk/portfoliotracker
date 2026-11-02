"""Migration 041 preservation and database-enforced identity boundaries."""
import importlib.util
import os
from pathlib import Path
import unittest
import uuid

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "requires disposable PostgreSQL TEST_DATABASE_URL")
class TestIdentityMigration(unittest.TestCase):
    def setUp(self):
        url = make_url(os.environ["TEST_DATABASE_URL"])
        if url.get_backend_name() != "postgresql" or url.host not in {"localhost", "127.0.0.1"} or not (url.database or "").endswith("_test"):
            raise RuntimeError("Requires a local disposable PostgreSQL database ending in _test")
        engine = create_engine(url)
        self.addCleanup(engine.dispose)
        self.connection = engine.connect()
        self.addCleanup(self.connection.close)
        transaction = self.connection.begin()
        self.addCleanup(transaction.rollback)
        schema = "identity_test_" + uuid.uuid4().hex
        self.execute(f'CREATE SCHEMA "{schema}"')
        self.execute(f'SET LOCAL search_path TO "{schema}"')
        self.execute("CREATE TABLE users (id INTEGER PRIMARY KEY)")
        self.execute("INSERT INTO users VALUES (1), (2)")
        self.execute("CREATE TABLE platforms (id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id), platform_type TEXT)")
        self.execute("INSERT INTO platforms VALUES (10, 1, 't212_api'), (20, 1, 't212_csv')")
        definitions = (
            ("orders", "pk", "id TEXT", "id", "uq_orders_user_t212"),
            ("transactions", "pk", "id TEXT", "id", "uq_transactions_user_t212"),
            ("dividend_payments", "id", "reference TEXT", "reference", "uq_dividend_payments_user_ref"),
            ("pies", "pk", "id INTEGER", "id", "uq_pies_user_t212"),
        )
        for table, pk, key_definition, key, constraint in definitions:
            self.execute(f"""CREATE TABLE {table} ({pk} INTEGER PRIMARY KEY, {key_definition},
                user_id INTEGER REFERENCES users(id), platform_id INTEGER REFERENCES platforms(id),
                amount NUMERIC, CONSTRAINT {constraint} UNIQUE (user_id, {key}))""")
            value = "7" if table == "pies" else "'shared'"
            self.execute(f"INSERT INTO {table} VALUES (100, {value}, 1, 10, 12.345)")
        self.execute("INSERT INTO transactions VALUES (101, 'unassigned', 1, NULL, 99)")
        self.execute("CREATE TABLE pie_holdings (id INTEGER PRIMARY KEY, pie_id INTEGER REFERENCES pies(pk))")
        self.execute("INSERT INTO pie_holdings VALUES (200, 100)")
        self.execute("CREATE TABLE trade_import_observations (id INTEGER PRIMARY KEY, order_pk INTEGER REFERENCES orders(pk))")
        self.execute("INSERT INTO trade_import_observations VALUES (300, 100)")

    def execute(self, sql):
        return self.connection.execute(text(sql))

    def upgrade(self):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        path = Path(__file__).resolve().parents[1] / "alembic/versions/041_account_scoped_identity.py"
        spec = importlib.util.spec_from_file_location("identity_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        with Operations.context(MigrationContext.configure(self.connection)):
            migration.upgrade()

    def test_preserves_internal_ids_values_and_links_and_allows_distinct_accounts(self):
        self.upgrade()
        for table, pk, key in (("orders", "pk", "id"), ("transactions", "pk", "id"),
                               ("dividend_payments", "id", "reference"), ("pies", "pk", "id")):
            row = self.execute(f"SELECT {pk}, {key}, user_id, platform_id, amount, source_namespace FROM {table} WHERE {pk} = 100").one()
            self.assertEqual(tuple(row[:4]), (100, 7 if table == "pies" else "shared", 1, 10))
            self.assertEqual(str(row.amount), "12.345")
            self.assertEqual(row.source_namespace, "t212")
            value = "7" if table == "pies" else "'shared'"
            self.execute(f"INSERT INTO {table} VALUES (102, {value}, 1, 20, 20, 't212')")
            self.execute(f"INSERT INTO {table} VALUES (103, {value}, 1, 10, 30, 'ii')")
            with self.assertRaises(IntegrityError):
                with self.connection.begin_nested():
                    self.execute(f"INSERT INTO {table} VALUES (104, {value}, 1, 10, 40, 't212')")
        self.assertEqual(self.execute("SELECT pie_id FROM pie_holdings WHERE id=200").scalar_one(), 100)
        self.assertEqual(self.execute("SELECT order_pk FROM trade_import_observations WHERE id=300").scalar_one(), 100)
        self.assertEqual(tuple(self.execute("SELECT platform_id, source_namespace FROM transactions WHERE pk=101").one()), (None, "legacy"))

    def test_existing_ownership_conflict_aborts_without_reassigning_rows(self):
        self.execute("UPDATE orders SET user_id=2 WHERE pk=100")
        with self.assertRaisesRegex(RuntimeError, "ownership conflicts"):
            with self.connection.begin_nested():
                self.upgrade()
        self.assertEqual(self.execute("SELECT user_id FROM orders WHERE pk=100").scalar_one(), 2)
        self.assertEqual(self.execute("""SELECT COUNT(*) FROM information_schema.columns
            WHERE table_schema = current_schema() AND table_name='orders' AND column_name='source_namespace'""").scalar_one(), 0)

    def test_new_cross_user_account_assignment_is_rejected(self):
        self.upgrade()
        for user_id, platform_id in (("2", "10"), ("NULL", "10"), ("1", "NULL")):
            with self.subTest(user=user_id, account=platform_id), self.assertRaises(IntegrityError):
                with self.connection.begin_nested():
                    self.execute(f"INSERT INTO orders VALUES (102, 'different', {user_id}, {platform_id}, 20, 't212')")
