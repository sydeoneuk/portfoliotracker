import importlib.util
import os
import unittest
from pathlib import Path
from sqlalchemy import create_engine, text
from alembic.migration import MigrationContext
from alembic.operations import Operations


@unittest.skipUnless(os.environ.get('TEST_DATABASE_URL'), 'requires disposable PostgreSQL')
class TestSnapshotEvidenceMigration(unittest.TestCase):
    def test_upgrade_preserves_legacy_values_without_inventing_units(self):
        from sqlalchemy.engine import make_url
        url = make_url(os.environ['TEST_DATABASE_URL'])
        assert url.host in {'localhost', '127.0.0.1'} and url.database.endswith('_test')
        engine = create_engine(url)
        try:
            with engine.connect() as db:
                transaction = db.begin()
                try:
                    db.execute(text('CREATE SCHEMA evidence_migration_test'))
                    db.execute(text('SET LOCAL search_path TO evidence_migration_test'))
                    db.execute(text('CREATE TABLE holding_snapshots (id integer, price_native float, value_gbp float)'))
                    db.execute(text('INSERT INTO holding_snapshots VALUES (1, 2000, 140)'))
                    spec = importlib.util.spec_from_file_location('migration047', Path('alembic/versions/047_snapshot_valuation_evidence.py'))
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    with Operations.context(MigrationContext.configure(db)):
                        module.upgrade()
                    row = db.execute(text('SELECT * FROM holding_snapshots')).one()
                    self.assertEqual(tuple(row), (1, 2000, 140, None, None, None, None))
                finally:
                    transaction.rollback()
        finally:
            engine.dispose()
