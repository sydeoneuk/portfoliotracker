import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from sqlalchemy.dialects import postgresql

from app.snapshots import snapshot_csv_platforms


class TestSnapshotConflictTarget(unittest.TestCase):
    def test_daily_snapshot_infers_columns_instead_of_removed_constraint(self):
        db = MagicMock()
        platform = SimpleNamespace(id=7, user_id=42, account_type="ISA")
        position = SimpleNamespace(ticker="TEST", quantity=2, currency="GBP", price_native=10)
        db.execute.return_value.fetchall.side_effect = [[platform], [position]]

        result = snapshot_csv_platforms(db)

        upsert = db.execute.call_args_list[3].args[0]
        sql = str(upsert.compile(dialect=postgresql.dialect()))
        self.assertIn("ON CONFLICT (user_id, snapshot_date, platform_id, ticker) DO UPDATE", sql)
        self.assertNotIn("ON CONSTRAINT", sql)
        self.assertEqual(result, {"platforms": 1, "snapshots_written": 1})
        db.commit.assert_called_once()
