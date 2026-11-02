"""Preservation checks for revision 039 in transaction-local test schemas."""
import importlib.util
import os
from pathlib import Path
import unittest
import uuid

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "requires disposable PostgreSQL TEST_DATABASE_URL")
class TestSnapshotMigration(unittest.TestCase):
    def setUp(self):
        url = make_url(os.environ["TEST_DATABASE_URL"])
        if (url.get_backend_name() != "postgresql" or url.host not in {"localhost", "127.0.0.1"}
                or not (url.database or "").endswith("_test")):
            raise RuntimeError("Requires a local disposable PostgreSQL database ending in _test")
        self.engine = create_engine(url)
        self.addCleanup(self.engine.dispose)
        self.connection = self.engine.connect()
        self.addCleanup(self.connection.close)
        transaction = self.connection.begin()
        self.addCleanup(transaction.rollback)
        schema = "migration_test_" + uuid.uuid4().hex
        self.connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        self.connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
        for table in ("holding_snapshots", "portfolio_snapshots"):
            self.connection.execute(text(f"""
                CREATE TABLE {table} (
                    id INTEGER PRIMARY KEY, user_id INTEGER, snapshot_date DATE,
                    account TEXT, platform_id INTEGER, ticker TEXT, scope_type TEXT,
                    pie_id INTEGER, quantity NUMERIC, total_value_gbp NUMERIC
                )
            """))
            self.connection.execute(text(f"""
                INSERT INTO {table} VALUES
                    (1, 1, '2026-11-01', 'ISA', 10, 'ABC', 'account', NULL, 3, 30),
                    (2, 1, '2026-11-01', 'Trading', 20, 'ABC', 'account', NULL, 4, 40),
                    (3, 1, '2026-10-31', 'ISA', NULL, 'ABC', 'account', NULL, 5, 50)
            """))
        self.connection.execute(text("""
            CREATE UNIQUE INDEX uq_holding_snapshots_daily_ticker
            ON holding_snapshots (user_id, snapshot_date, account, ticker)
        """))
        self.connection.execute(text("""
            CREATE UNIQUE INDEX uq_portfolio_snapshots_daily_scope
            ON portfolio_snapshots (user_id, snapshot_date, account, scope_type, COALESCE(pie_id, -1))
        """))

    def upgrade(self):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        path = Path(__file__).resolve().parents[1] / "alembic/versions/039_snapshot_platform_indexes.py"
        spec = importlib.util.spec_from_file_location("snapshot_platform_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        with Operations.context(MigrationContext.configure(self.connection)):
            migration.upgrade()

    def snapshot_rows(self):
        return {table: self.connection.execute(text(f"SELECT * FROM {table} ORDER BY id")).all()
                for table in ("holding_snapshots", "portfolio_snapshots")}

    def test_upgrade_preserves_every_value_including_unresolved_rows(self):
        before = self.snapshot_rows()
        self.upgrade()
        self.assertEqual(self.snapshot_rows(), before)
        # Same wrapper and ticker on a different platform must remain independent.
        self.connection.execute(text("""
            INSERT INTO holding_snapshots VALUES
                (4, 1, '2026-11-01', 'ISA', 30, 'ABC', 'account', NULL, 9, 90)
        """))

    def test_duplicate_platform_keys_abort_without_changing_rows_or_old_indexes(self):
        self.connection.execute(text("UPDATE holding_snapshots SET platform_id = 10 WHERE id = 2"))
        before = self.snapshot_rows()
        with self.assertRaises(IntegrityError):
            with self.connection.begin_nested():
                self.upgrade()
        self.assertEqual(self.snapshot_rows(), before)
        self.assertIsNotNone(self.connection.execute(text(
            "SELECT to_regclass('uq_holding_snapshots_daily_ticker')"
        )).scalar_one())
        self.assertIsNone(self.connection.execute(text(
            "SELECT to_regclass('uq_holding_snapshots_platform_daily')"
        )).scalar_one())

    def test_trade_migration_preserves_legacy_values_and_leaves_provenance_unknown(self):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        self.connection.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY)"))
        self.connection.execute(text("CREATE TABLE orders (pk INTEGER PRIMARY KEY, fill_price DOUBLE PRECISION, filled_quantity DOUBLE PRECISION)"))
        self.connection.execute(text("CREATE TABLE positions (ticker TEXT PRIMARY KEY, quantity DOUBLE PRECISION, average_price DOUBLE PRECISION)"))
        self.connection.execute(text("INSERT INTO orders VALUES (1, 80.125, 10.5)"))
        self.connection.execute(text("INSERT INTO positions VALUES ('ABC', 10.5, 80.125)"))
        path = Path(__file__).resolve().parents[1] / "alembic/versions/040_trade_cost_provenance.py"
        spec = importlib.util.spec_from_file_location("trade_cost_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        with Operations.context(MigrationContext.configure(self.connection)):
            migration.upgrade()
        self.assertEqual(tuple(self.connection.execute(text(
            "SELECT pk, fill_price, filled_quantity, execution_price, execution_currency FROM orders"
        )).one()), (1, 80.125, 10.5, None, None))
        self.assertEqual(tuple(self.connection.execute(text(
            "SELECT ticker, quantity, average_price, cost_basis_amount, cost_basis_status FROM positions"
        )).one()), ("ABC", 10.5, 80.125, None, None))
