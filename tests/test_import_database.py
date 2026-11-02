"""Opt-in PostgreSQL regressions. Run only against a migrated disposable DB.

TEST_DATABASE_URL must explicitly name a local database ending in `_test`.
The deployment CI supplies this using a temporary PostgreSQL service.
"""
import datetime
import io
import os
import unittest
import uuid
from unittest.mock import patch

from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.auth.models import User
from app.importers.ii_transactions_csv import IiTransactionsCsvImporter
from app.importers.t212_csv import T212CsvImporter
from app.importers.ii_csv import IiCsvImporter
from app.importers.t212_pie_csv import T212PieCsvImporter
from app.models.pie import Pie, PieHolding
from app.models import Base, HoldingSnapshot, Instrument, Platform, Position, Transaction
from app.models import InstrumentPrice
from app.valuation import ValuationUnavailable, effective_price_sql
from app.snapshots import snapshot_csv_platforms


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "requires disposable PostgreSQL TEST_DATABASE_URL")
class TestImportDatabase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        url = make_url(os.environ["TEST_DATABASE_URL"])
        if (url.get_backend_name() != "postgresql"
                or url.host not in {"localhost", "127.0.0.1"}
                or not (url.database or "").endswith("_test")):
            raise RuntimeError("Integration tests require a local PostgreSQL database ending in _test")
        cls.engine = create_engine(url)
        from alembic.config import Config
        from alembic.script import ScriptDirectory
        expected_revision = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
        with cls.engine.connect() as connection:
            revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            if revision != expected_revision:
                raise RuntimeError("Run alembic upgrade head on the disposable test database first")

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        self.db = Session(self.engine)
        self.addCleanup(self.db.close)
        self.ticker = "test_" + uuid.uuid4().hex[:20]
        user = User(email=self.ticker + "@example.invalid", provider="test", provider_id=self.ticker)
        self.db.add(user)
        self.db.flush()
        self.user_id = user.id
        self.platform = Platform(user_id=user.id, name=self.ticker,
                                 platform_type="interactive_investor", account_type="ISA", currency="GBP")
        self.db.add(self.platform)
        self.db.add(Instrument(ticker=self.ticker, currency_code="GBP"))
        self.db.commit()
        self.platform_id = self.platform.id
        self.addCleanup(self.cleanup_records)

    def cleanup_records(self):
        self.db.rollback()
        # Delete only this test user's rows; never truncate or reset a database.
        for table in reversed(Base.metadata.sorted_tables):
            if "user_id" in table.c:
                self.db.execute(delete(table).where(table.c.user_id == self.user_id))
        self.db.execute(delete(User).where(User.id == self.user_id))
        self.db.execute(delete(Instrument).where(Instrument.ticker == self.ticker))
        for ticker in getattr(self, "extra_instruments", []):
            self.db.execute(delete(Instrument).where(Instrument.ticker == ticker))
        self.db.commit()

    def transaction_csv(self):
        return io.BytesIO(
            b"Date,Symbol,Description,Reference,Debit,Credit\n"
            b"25/10/2026,n/a,GROSS INTEREST,test-reference,n/a,1.05\n"
        )

    def test_ii_import_survives_session_close_and_repeat_upload(self):
        for _ in range(2):
            result = IiTransactionsCsvImporter(self.db, self.platform, self.user_id).import_file(self.transaction_csv())
            self.assertEqual(result.errors, [])
        self.db.close()
        with Session(self.engine) as reader:
            rows = reader.scalars(select(Transaction).where(Transaction.user_id == self.user_id)).all()
            self.assertEqual(len(rows), 1)
            self.assertAlmostEqual(rows[0].amount, 1.05)
            self.assertEqual(rows[0].platform_id, self.platform_id)

    def test_ii_database_failure_preserves_existing_data(self):
        importer = IiTransactionsCsvImporter(self.db, self.platform, self.user_id)
        importer.import_file(self.transaction_csv())
        importer = IiTransactionsCsvImporter(self.db, self.platform, self.user_id)
        original_process = importer._process_row

        def fail_after_write(row, row_num):
            original_process(row, row_num)
            self.db.execute(text("SELECT 1 / 0"))

        failed_csv = io.BytesIO(self.transaction_csv().getvalue().replace(b"test-reference", b"new-reference"))
        with patch.object(importer, "_process_row", side_effect=fail_after_write):
            result = importer.import_file(failed_csv)
        self.assertTrue(result.errors)
        self.assertEqual(result.transactions_upserted, 0)
        with Session(self.engine) as reader:
            rows = reader.scalars(select(Transaction).where(Transaction.user_id == self.user_id)).all()
            self.assertEqual([row.id for row in rows], ["test-reference"])

    def test_t212_rebuild_failure_preserves_positions_and_rolls_back_orders(self):
        self.db.add(Position(ticker=self.ticker, platform_id=self.platform_id,
                             user_id=self.user_id, quantity=7, average_price=12))
        self.db.commit()
        csv = io.StringIO(
            "Action,Time (UTC),ID,Ticker,No. of shares,Price / share,Total\n"
            f"Market buy,2026-10-25 12:00:00,new-order,{self.ticker},2,10,20\n"
        )
        importer = T212CsvImporter(self.db, self.platform, self.user_id)
        with patch.object(importer, "_rebuild_positions", side_effect=RuntimeError("failed rebuild")):
            result = importer.import_file(csv)
        self.assertTrue(result.errors)
        with Session(self.engine) as reader:
            position = reader.get(Position, (self.ticker, self.platform_id, self.user_id))
            self.assertEqual(position.quantity, 7)
            self.assertEqual(reader.execute(text("SELECT COUNT(*) FROM orders WHERE user_id = :uid"),
                                            {"uid": self.user_id}).scalar_one(), 0)

    def test_csv_snapshot_updates_migrated_unique_index(self):
        with self.engine.connect() as connection:
            constraint_count = connection.execute(text(
                "SELECT COUNT(*) FROM pg_constraint WHERE conname = 'uq_holding_snapshots_daily_ticker'"
            )).scalar_one()
            self.assertEqual(constraint_count, 0)  # migration 033 creates an index instead
        self.db.add(Position(ticker=self.ticker, platform_id=self.platform_id,
                             user_id=self.user_id, quantity=2, average_price=10, current_price=10))
        self.db.commit()
        snapshot_csv_platforms(self.db)
        position = self.db.get(Position, (self.ticker, self.platform_id, self.user_id))
        position.quantity = 3
        self.db.commit()
        snapshot_csv_platforms(self.db)
        with Session(self.engine) as reader:
            rows = reader.scalars(select(HoldingSnapshot).where(
                HoldingSnapshot.user_id == self.user_id,
                HoldingSnapshot.snapshot_date == datetime.date.today(),
            )).all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].quantity, 3)
            self.assertEqual(rows[0].value_gbp, 30)

    def test_same_wrapper_accounts_keep_independent_snapshots(self):
        other = Platform(user_id=self.user_id, name=self.ticker + "-second",
                         platform_type="interactive_investor", account_type="ISA", currency="GBP")
        self.db.add(other)
        self.db.flush()
        for platform_id, quantity in [(self.platform_id, 2), (other.id, 5)]:
            self.db.add(Position(ticker=self.ticker, platform_id=platform_id,
                                 user_id=self.user_id, quantity=quantity, average_price=10, current_price=10))
        self.db.commit()
        snapshot_csv_platforms(self.db)
        snapshot_csv_platforms(self.db)
        with Session(self.engine) as reader:
            rows = reader.scalars(select(HoldingSnapshot).where(
                HoldingSnapshot.user_id == self.user_id,
            )).all()
            self.assertEqual(sorted(row.quantity for row in rows), [2, 5])
            totals = reader.execute(text(
                "SELECT total_value_gbp FROM portfolio_snapshots WHERE user_id = :uid"
            ), {"uid": self.user_id}).scalars().all()
            self.assertEqual(sorted(totals), [20, 50])

    def test_ii_malformed_snapshot_preserves_existing_position(self):
        self.db.add(Position(ticker=self.ticker, platform_id=self.platform_id,
                             user_id=self.user_id, quantity=7, average_price=12))
        self.db.commit()
        csv = io.StringIO(f"Symbol,Qty,Price\n{self.ticker},2,£10\nBROKEN,invalid,£10\n")
        result = IiCsvImporter(self.db, self.platform, self.user_id).import_file(csv)
        self.assertTrue(result.errors)
        with Session(self.engine) as reader:
            position = reader.get(Position, (self.ticker, self.platform_id, self.user_id))
            self.assertEqual((position.quantity, position.average_price), (7, 12))

    def test_ii_valid_snapshot_updates_only_selected_account(self):
        other = Platform(user_id=self.user_id, name=self.ticker + "-second",
                         platform_type="interactive_investor", account_type="ISA", currency="GBP")
        self.db.add(other)
        self.db.flush()
        self.db.add(Position(ticker=self.ticker, platform_id=other.id,
                             user_id=self.user_id, quantity=7, average_price=12))
        self.db.commit()
        other_id = other.id
        csv = io.StringIO(f"Symbol,Qty,Price,Average Price\n{self.ticker},2,£10,£8\n")
        result = IiCsvImporter(self.db, self.platform, self.user_id).import_file(csv)
        self.assertEqual(result.errors, [])
        with Session(self.engine) as reader:
            own = reader.get(Position, (self.ticker, self.platform_id, self.user_id))
            other_position = reader.get(Position, (self.ticker, other_id, self.user_id))
            self.assertEqual((own.quantity, own.average_price, own.current_price), (2, 8, 10))
            self.assertEqual(other_position.quantity, 7)

    def test_pie_invalid_and_failed_replacement_preserve_old_holdings(self):
        def importer():
            return T212PieCsvImporter(self.db, self.platform, self.user_id, "Test pie")

        def csv(quantity):
            return io.StringIO(f"Slice,Quantity\n{self.ticker},{quantity}\n")

        result = importer().import_file(csv(7))
        self.assertEqual(result.errors, [])
        for invalid in (csv("bad"), io.StringIO(f"Slice,Quantity\n{self.ticker},2\nBROKEN,nan\n")):
            self.assertTrue(importer().import_file(invalid).errors)
        failed = importer()
        with patch.object(failed, "_reconcile", side_effect=RuntimeError("reconciliation failed")):
            result = failed.import_file(csv(2))
        self.assertTrue(result.errors)
        with Session(self.engine) as reader:
            holdings = reader.scalars(select(PieHolding).where(PieHolding.user_id == self.user_id)).all()
            self.assertEqual([holding.owned_quantity for holding in holdings], [7])
        self.assertEqual(importer().import_file(csv(3)).errors, [])
        with Session(self.engine) as reader:
            holdings = reader.scalars(select(PieHolding).where(PieHolding.user_id == self.user_id)).all()
            self.assertEqual([holding.owned_quantity for holding in holdings], [3])

    def test_csv_imports_preserve_existing_isin(self):
        instrument = self.db.get(Instrument, self.ticker)
        instrument.isin = "GB0000000001"
        self.db.commit()
        for incoming_isin in ("", "GB0000000002"):
            T212CsvImporter(self.db, self.platform, self.user_id)._ensure_instrument(
                incoming_isin, self.ticker, "Test", "GBP")
            self.db.commit()
            T212PieCsvImporter(self.db, self.platform, self.user_id, "Test pie")._ensure_instrument(
                incoming_isin, self.ticker, "Test", "GBP")
            self.db.commit()
        with Session(self.engine) as reader:
            self.assertEqual(reader.get(Instrument, self.ticker).isin, "GB0000000001")

    def seed_valuation(self, currency, price, gbp, source="yfinance", days_old=0):
        self.db.get(Instrument, self.ticker).currency_code = currency
        self.db.add(InstrumentPrice(
            ticker=self.ticker, source=source, price=price, price_gbp=gbp, currency=currency,
            updated_at=datetime.datetime.utcnow() - datetime.timedelta(days=days_old),
        ))
        if self.db.get(Position, (self.ticker, self.platform_id, self.user_id)) is None:
            self.db.add(Position(ticker=self.ticker, platform_id=self.platform_id, user_id=self.user_id,
                                 quantity=10, average_price=1))
        self.db.commit()

    def test_native_quote_ignores_gbp_equivalent_and_prefers_fresh_quote(self):
        self.seed_valuation("GBX", 1500, 15, "t212_api", days_old=3)
        self.seed_valuation("GBX", 2000, 20)
        native = self.db.execute(text(
            f"SELECT {effective_price_sql()} FROM positions p WHERE p.user_id = :uid"
        ), {"uid": self.user_id}).scalar_one()
        self.assertEqual(native, 2000)
        snapshot_csv_platforms(self.db)
        self.assertEqual(self.db.scalar(select(HoldingSnapshot.value_gbp).where(
            HoldingSnapshot.user_id == self.user_id)), 200)

    def test_usd_dashboard_analysis_and_csv_snapshot_agree(self):
        from app.web import server
        from starlette.requests import Request
        self.seed_valuation("USD", 100, 80)
        rates = {"GBP": 1, "USD": 0.8}
        request = Request({"type": "http", "method": "GET", "path": "/portfolio",
                           "headers": [], "query_string": b"", "session": {"user_id": self.user_id}})
        with patch.object(server, "_get_fx_rates_to_gbp", return_value=rates):
            response = server.index(request, self.db)
            self.assertEqual(response.context["total_value"], 800)
            for account in ("combined", f"platform_{self.platform_id}", "ISA"):
                positions = server._normalise_positions_for_display(
                    server._fetch_filtered_positions(self.db, self.user_id, account, []))
                context = server._build_position_display_context(self.db, self.user_id, account, positions)
                self.assertEqual(context["grand_total_display"]["value"], 800)
        with patch("app.snapshots.get_fx_rates_to_gbp", return_value=rates):
            snapshot_csv_platforms(self.db)
        self.assertEqual(self.db.scalar(select(HoldingSnapshot.value_gbp).where(
            HoldingSnapshot.user_id == self.user_id)), 800)

    def test_missing_fx_preserves_previous_daily_snapshot(self):
        self.seed_valuation("USD", 100, None)
        with patch("app.snapshots.get_fx_rates_to_gbp", return_value={"USD": 0.8}):
            snapshot_csv_platforms(self.db)
        with patch("app.snapshots.get_fx_rates_to_gbp", return_value={}):
            with self.assertRaises(ValuationUnavailable):
                snapshot_csv_platforms(self.db)
        with Session(self.engine) as reader:
            self.assertEqual(reader.scalar(select(HoldingSnapshot.value_gbp).where(
                HoldingSnapshot.user_id == self.user_id)), 800)

    def test_cost_is_not_used_as_a_market_price(self):
        self.db.add(Position(ticker=self.ticker, platform_id=self.platform_id, user_id=self.user_id,
                             quantity=10, average_price=12))
        self.db.commit()
        with self.assertRaises(ValuationUnavailable):
            snapshot_csv_platforms(self.db)

    def test_pence_dashboard_and_analysis_are_not_divided_twice(self):
        from app.web import server
        from starlette.requests import Request
        self.seed_valuation("GBX", 2000, 20)
        request = Request({"type": "http", "method": "GET", "path": "/portfolio",
                           "headers": [], "query_string": b"", "session": {"user_id": self.user_id}})
        response = server.index(request, self.db)
        self.assertEqual(response.context["total_value"], 200)
        positions = server._normalise_positions_for_display(
            server._fetch_filtered_positions(self.db, self.user_id, "combined", []))
        context = server._build_position_display_context(self.db, self.user_id, "combined", positions)
        self.assertEqual(context["grand_total_display"]["value"], 200)

    def test_api_and_csv_snapshot_valuations_agree(self):
        from app.sync.runner import SyncRunner
        self.seed_valuation("USD", 100, 80)
        runner = SyncRunner.__new__(SyncRunner)
        runner.session, runner.user_id = self.db, self.user_id
        runner.platform, runner.platform_id = self.platform, self.platform_id
        runner.account = "Test"
        with patch("app.sync.runner.get_fx_rates_to_gbp", return_value={"USD": 0.8}):
            runner.snapshot_portfolio_day()
        self.assertEqual(self.db.scalar(select(HoldingSnapshot.value_gbp).where(
            HoldingSnapshot.user_id == self.user_id)), 800)

    def test_pie_and_outside_pie_values_use_same_native_quote(self):
        from app.web import server
        self.seed_valuation("USD", 100, 80)
        pie = Pie(id=1, user_id=self.user_id, platform_id=self.platform_id, name="Test")
        self.db.add(pie)
        self.db.flush()
        self.db.add(PieHolding(user_id=self.user_id, pie_id=pie.pk, ticker=self.ticker, owned_quantity=2))
        self.db.commit()
        for selected, outside, expected in (([pie.pk], False, 200), ([], True, 800), ([pie.pk], True, 1000)):
            rows = server._fetch_filtered_positions(self.db, self.user_id,
                                                    f"platform_{self.platform_id}", selected, outside)
            self.assertEqual(sum(float(row["value"]) for row in rows), expected)

    def test_same_day_snapshot_removes_sold_holdings_and_records_zero(self):
        self.seed_valuation("GBP", 100, 100)
        snapshot_csv_platforms(self.db)
        self.db.execute(delete(Position).where(Position.user_id == self.user_id))
        self.db.commit()
        snapshot_csv_platforms(self.db)
        with Session(self.engine) as reader:
            rows = reader.scalars(select(HoldingSnapshot).where(HoldingSnapshot.user_id == self.user_id)).all()
            self.assertEqual(rows, [])
            self.assertEqual(reader.execute(text(
                "SELECT total_value_gbp FROM portfolio_snapshots WHERE user_id = :uid"
            ), {"uid": self.user_id}).scalar_one(), 0)

    def test_quote_units_can_differ_from_listing_units(self):
        self.seed_valuation("GBX", 2000, 20)
        self.db.get(Instrument, self.ticker).currency_code = "GBP"
        self.db.commit()
        snapshot_csv_platforms(self.db)
        snapshot = self.db.scalar(select(HoldingSnapshot).where(HoldingSnapshot.user_id == self.user_id))
        self.assertEqual(snapshot.price_native, 20)
        self.assertEqual(snapshot.value_gbp, 200)

    def test_partial_price_coverage_does_not_produce_partial_portfolio_total(self):
        from app.web import server
        other = Platform(user_id=self.user_id, name="Other", platform_type="interactive_investor",
                         account_type="ISA", currency="GBP")
        self.db.add(other)
        self.db.flush()
        for platform_id, price in ((self.platform_id, 10), (other.id, None)):
            self.db.add(Position(user_id=self.user_id, platform_id=platform_id, ticker=self.ticker,
                                 quantity=2, current_price=price, average_price=1))
        self.db.commit()
        positions = server._fetch_filtered_positions(self.db, self.user_id, "combined", [])
        with self.assertRaises(ValuationUnavailable):
            server._normalise_positions_for_display(positions)

    def test_yahoo_reports_own_quote_currency_and_retains_native_price_without_fx(self):
        from app.prices import yfinance_provider as provider
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        raw = MagicMock()
        raw.__getitem__.return_value.dropna.return_value.iloc.__getitem__.return_value = 2000
        instrument = {"ticker": self.ticker, "yf_ticker": "TEST.L", "currency": "GBP"}
        with patch.object(provider.yf, "download", return_value=raw):
            with patch.object(provider.yf, "Ticker", return_value=SimpleNamespace(fast_info={"currency": "GBp"})):
                result = provider.YFinanceProvider().fetch([instrument])[0]
                self.assertEqual((result.currency, result.price, result.price_gbp), ("GBX", 2000, 20))
            with patch.object(provider.yf, "Ticker", return_value=SimpleNamespace(fast_info={"currency": "USD"})):
                with patch.object(provider, "_fetch_fx_rates", return_value={}):
                    result = provider.YFinanceProvider().fetch([instrument])[0]
                self.assertEqual(result.currency, "USD")
                self.assertIsNone(result.price_gbp)

    def trade_csv(self, rows):
        import csv
        stream = io.StringIO()
        writer = csv.writer(stream)
        writer.writerow(["Action", "Time (UTC)", "ID", "Ticker", "No. of shares", "Price / share",
                         "Currency (Price / share)", "Total", "Currency (Total)", "Transaction fee",
                         "Currency (Transaction fee)"])
        for i, (action, quantity, price, total, fee) in enumerate(rows, 1):
            writer.writerow([action, f"2026-01-{i:02d} 12:00:00", f"trade-{i}", self.ticker,
                             quantity, price, "USD", total, "GBP", fee, "GBP"])
        stream.seek(0)
        return stream

    def test_chronological_cost_survives_reimports_and_keeps_raw_trade_rows(self):
        from app.models.order import TradeImportObservation
        rows = [("Market buy", 10, 10, 102, 2), ("Market sell", 10, 15, 149, 1),
                ("Market buy", 10, 20, 202, 2)]
        for _ in range(2):
            result = T212CsvImporter(self.db, self.platform, self.user_id).import_file(self.trade_csv(rows))
            self.assertEqual(result.errors, [])
        with Session(self.engine) as reader:
            position = reader.get(Position, (self.ticker, self.platform_id, self.user_id))
            self.assertEqual((position.quantity, position.cost_basis_amount, position.cost_basis_currency), (10, 202, "GBP"))
            self.assertEqual(position.cost_basis_status, "estimated")
            self.assertIsNone(position.average_price)  # no invented broker average
            observations = reader.scalars(select(TradeImportObservation).where(
                TradeImportObservation.user_id == self.user_id)).all()
            self.assertEqual(len(observations), 3)
            self.assertEqual(observations[0].payload["Currency (Price / share)"], "USD")

    def test_reimport_preserves_legacy_price_average_and_quantity(self):
        from app.models import Order
        self.db.add(Order(source_namespace="t212", id="trade-1", user_id=self.user_id, platform_id=self.platform_id, ticker=self.ticker,
                          side="BUY", status="FILLED", fill_price=80, filled_quantity=10,
                          filled_at=datetime.datetime(2026, 1, 1, 12)))
        self.db.add(Position(ticker=self.ticker, platform_id=self.platform_id, user_id=self.user_id,
                             quantity=10, average_price=80))
        self.db.commit()
        result = T212CsvImporter(self.db, self.platform, self.user_id).import_file(
            self.trade_csv([("Market buy", 10, 100, 800, 0)]))
        self.assertEqual(result.errors, [])
        with Session(self.engine) as reader:
            order = reader.scalar(select(Order).where(Order.user_id == self.user_id))
            position = reader.get(Position, (self.ticker, self.platform_id, self.user_id))
            self.assertEqual((order.fill_price, order.execution_price, order.execution_currency), (80, 100, "USD"))
            self.assertEqual((position.quantity, position.average_price), (10, 80))
            self.assertEqual(position.cost_basis_amount, 800)

    def test_incomplete_history_preserves_legacy_holding(self):
        self.db.add(Position(ticker=self.ticker, platform_id=self.platform_id, user_id=self.user_id,
                             quantity=7, average_price=12))
        self.db.commit()
        result = T212CsvImporter(self.db, self.platform, self.user_id).import_file(
            self.trade_csv([("Market sell", 3, 10, 30, 0)]))
        self.assertEqual(result.errors, [])
        self.assertTrue(result.warnings)
        with Session(self.engine) as reader:
            position = reader.get(Position, (self.ticker, self.platform_id, self.user_id))
            self.assertEqual((position.quantity, position.average_price), (7, 12))
            self.assertIsNone(position.cost_basis_amount)

    def test_cross_account_trade_id_cannot_move_or_overwrite_order(self):
        from app.models import Order
        other = Platform(user_id=self.user_id, name="Other", platform_type="t212_csv", account_type="SIPP", currency="GBP")
        self.db.add(other)
        self.db.flush()
        self.db.add(Order(source_namespace="t212", id="trade-1", ticker=self.ticker, user_id=self.user_id, platform_id=other.id,
                          side="BUY", status="FILLED", fill_price=123))
        self.db.commit()
        other_id = other.id
        result = T212CsvImporter(self.db, self.platform, self.user_id).import_file(
            self.trade_csv([("Market buy", 2, 10, 20, 0)]))
        self.assertEqual(result.errors, [])
        with Session(self.engine) as reader:
            orders = reader.scalars(select(Order).where(Order.user_id == self.user_id)).all()
            self.assertEqual(len(orders), 2)
            order = next(o for o in orders if o.platform_id == other_id)
            self.assertEqual((order.platform_id, order.fill_price), (other_id, 123))

    def test_holding_dividends_and_metrics_render_without_trade_loop_variable(self):
        from types import SimpleNamespace
        from app.web import server
        from starlette.requests import Request
        request = Request({"type": "http", "method": "GET", "path": "/holding/test",
                           "headers": [], "query_string": b"", "session": {"user_id": self.user_id}})
        self.seed_valuation("USD", 100, 80)
        with patch.object(server, "_get_fx_rates_to_gbp", return_value={"GBP": 1, "USD": 0.8}):
            response = server.holding_detail(self.ticker, request, account=f"platform_{self.platform_id}", db=self.db)
        context = dict(response.context)
        context.update(annual_dps=1.2345, eps_ttm=2.3456, fcf_ps=3.4567, trade_history=[],
                       div_payments=[SimpleNamespace(paid_on=None, account="Test", quantity=1,
                         amount=1, amount_display=1, gross_amount_per_share=4.5678, type="DIVIDEND", reference="test")])
        formatted = []
        def capture(value, currency, precision=2):
            formatted.append((value, currency, precision))
            return str(value)
        context["cfmt"] = capture
        html = server.templates.get_template("holding.html").render(context)
        self.assertIn("Dividend Payments Received", html)
        for value in (1.2345, 2.3456, 3.4567, 4.5678):
            self.assertIn((value, "USD", 4), formatted)

    def test_cost_and_trade_views_use_explicit_currency_and_show_unknown_legacy_cost(self):
        from app.web import server
        from starlette.requests import Request
        request = Request({"type": "http", "method": "GET", "path": "/analysis",
                           "headers": [], "query_string": b"", "session": {"user_id": self.user_id}})
        self.seed_valuation("USD", 100, 80)
        with patch.object(server, "_get_fx_rates_to_gbp", return_value={"GBP": 1, "USD": 0.8}):
            response = server.portfolio_analysis(request, account=f"platform_{self.platform_id}", db=self.db)
            self.assertIsNone(response.context["analysis_summary"]["cost"])
            self.assertIsNone(response.context["analysis_summary"]["unrealised"])
            response = server.holding_detail(self.ticker, request, account=f"platform_{self.platform_id}", db=self.db)
            self.assertIsNone(response.context["cost_display"])
        result = T212CsvImporter(self.db, self.platform, self.user_id).import_file(
            self.trade_csv([("Market buy", 10, 100, 802, 2)]))
        self.assertEqual(result.errors, [])
        with patch.object(server, "_get_fx_rates_to_gbp", return_value={"GBP": 1, "USD": 0.8}):
            response = server.portfolio_analysis(request, account=f"platform_{self.platform_id}", db=self.db)
            self.assertEqual(response.context["analysis_summary"]["cost"], 802)
            self.assertEqual(response.context["analysis_summary"]["unrealised"], -2)
            self.assertTrue(response.context["costs_estimated"])
            response = server.transactions_page(request, db=self.db)
            self.assertEqual(response.context["total_buy_gbp"], 802)
            self.assertEqual(response.context["orders"][0]["fill_price"], 100)
            self.assertEqual(response.context["orders"][0]["currency"], "USD")

    def test_failed_cost_projection_rolls_back_trade_observations(self):
        from app.models.order import TradeImportObservation
        importer = T212CsvImporter(self.db, self.platform, self.user_id)
        with patch.object(importer, "_rebuild_positions", side_effect=RuntimeError("projection failed")):
            result = importer.import_file(self.trade_csv([("Market buy", 10, 100, 800, 0)]))
        self.assertTrue(result.errors)
        with Session(self.engine) as reader:
            self.assertEqual(reader.scalars(select(TradeImportObservation).where(
                TradeImportObservation.user_id == self.user_id)).all(), [])

    def test_ii_trade_retains_execution_settlement_and_sedol_without_rebuilding_holdings(self):
        from app.models import Order
        from app.models.order import TradeImportObservation
        self.db.add(Position(ticker=self.ticker, platform_id=self.platform_id, user_id=self.user_id,
                             quantity=7, average_price=12))
        self.db.commit()
        csv = (
            "Date,Settlement Date,Symbol,Sedol,Quantity,Price,Description,Reference,Debit,Credit\n"
            f"03/07/2026,07/07/2026,{self.ticker},BMZLQ03,2,£10,Buy,IIREF,£21,n/a\n"
        )
        for _ in range(2):
            result = IiTransactionsCsvImporter(self.db, self.platform, self.user_id).import_file(io.BytesIO(csv.encode()))
            self.assertEqual(result.errors, [])
        with Session(self.engine) as reader:
            order = reader.scalar(select(Order).where(Order.user_id == self.user_id))
            self.assertEqual((order.execution_price, order.execution_currency), (10, "GBP"))
            self.assertEqual((order.settlement_amount, order.settlement_date), (-21, datetime.date(2026, 7, 7)))
            observations = reader.scalars(select(TradeImportObservation).where(
                TradeImportObservation.user_id == self.user_id)).all()
            self.assertEqual(len(observations), 1)
            self.assertEqual(observations[0].payload["Sedol"], "BMZLQ03")
            self.assertEqual(reader.get(Position, (self.ticker, self.platform_id, self.user_id)).quantity, 7)

    def test_api_refresh_records_broker_cost_separately_from_legacy_average(self):
        from app.sync.runner import SyncRunner
        from types import SimpleNamespace
        runner = SyncRunner.__new__(SyncRunner)
        runner.session, runner.user_id = self.db, self.user_id
        runner.platform, runner.platform_id = self.platform, self.platform_id
        runner.client = SimpleNamespace(get_positions=lambda: [
            {"ticker": self.ticker, "quantity": 3, "averagePrice": 12, "currentPrice": 15}])
        runner.sync_positions()
        with Session(self.engine) as reader:
            position = reader.get(Position, (self.ticker, self.platform_id, self.user_id))
            self.assertEqual((position.average_price, position.cost_basis_amount), (12, 36))
            self.assertEqual((position.cost_basis_source, position.cost_basis_status), ("broker", "reported"))

    def test_api_open_order_sync_cannot_delete_or_move_other_account_order(self):
        from app.sync.runner import SyncRunner
        from app.models import Order
        from types import SimpleNamespace
        other = Platform(user_id=self.user_id, name="Other", platform_type="t212_api", account_type="ISA", currency="GBP")
        self.db.add(other)
        self.db.flush()
        self.db.add(Order(source_namespace="t212", id="123", ticker=self.ticker, user_id=self.user_id, platform_id=other.id,
                          side="BUY", status="LOCAL_OPEN", fill_price=11))
        self.db.commit()
        other_id = other.id
        runner = SyncRunner.__new__(SyncRunner)
        runner.session, runner.user_id = self.db, self.user_id
        runner.platform, runner.platform_id = self.platform, self.platform_id
        runner.client = SimpleNamespace(get_open_orders=lambda: [{"id": "123", "ticker": self.ticker,
                                                                  "side": "BUY", "fillPrice": 100}])
        runner._run_step(runner.sync_open_orders)
        with Session(self.engine) as reader:
            orders = reader.scalars(select(Order).where(Order.user_id == self.user_id)).all()
            self.assertEqual(len(orders), 2)
            order = next(o for o in orders if o.platform_id == other_id)
            self.assertEqual((order.platform_id, order.fill_price, order.status), (other_id, 11, "LOCAL_OPEN"))

    def test_explicit_dividend_and_cash_ids_are_independent_across_accounts(self):
        from app.models import DividendPayment
        other = Platform(user_id=self.user_id, name="Other", platform_type="t212_csv", account_type="ISA", currency="GBP")
        self.db.add(other)
        self.db.commit()
        for platform, amount in ((self.platform, 2), (other, 3), (self.platform, 2)):
            csv = io.StringIO(
                "Action,Time (UTC),ID,Ticker,No. of shares,Total\n"
                f"Dividend,2026-01-01 12:00:00,same-div,{self.ticker},10,{amount}\n"
                f"Deposit,2026-01-01 12:00:00,same-cash,,,{amount}\n")
            result = T212CsvImporter(self.db, platform, self.user_id).import_file(csv)
            self.assertEqual(result.errors, [])
        with Session(self.engine) as reader:
            dividends = reader.scalars(select(DividendPayment).where(DividendPayment.user_id == self.user_id)).all()
            cash = reader.scalars(select(Transaction).where(Transaction.user_id == self.user_id)).all()
            self.assertEqual(sorted(d.amount for d in dividends), [2, 3])
            self.assertEqual(sorted(t.amount for t in cash), [2, 3])
            self.assertEqual(len({d.id for d in dividends}), 2)

    def test_different_source_namespaces_can_reuse_cash_id_on_same_account(self):
        cash = io.StringIO("Action,ID,Time (UTC),Total\nDeposit,test-reference,2026-01-01 12:00:00,50\n")
        result = T212CsvImporter(self.db, self.platform, self.user_id).import_file(cash)
        self.assertEqual(result.errors, [])
        result = IiTransactionsCsvImporter(self.db, self.platform, self.user_id).import_file(self.transaction_csv())
        self.assertEqual(result.errors, [])
        with Session(self.engine) as reader:
            rows = reader.scalars(select(Transaction).where(Transaction.user_id == self.user_id)).all()
            self.assertEqual({t.source_namespace for t in rows}, {"t212", "ii"})
            self.assertEqual(sorted(t.amount for t in rows), [1.05, 50])

    def test_api_and_csv_order_overlap_reuses_internal_id(self):
        from app.models import Order
        from app.sync.runner import SyncRunner
        from unittest.mock import MagicMock
        self.db.get(Instrument, self.ticker).currency_code = "USD"
        self.db.commit()
        result = T212CsvImporter(self.db, self.platform, self.user_id).import_file(
            self.trade_csv([("Market buy", 10, 100, 800, 0)]))
        self.assertEqual(result.errors, [])
        pk = self.db.scalar(select(Order.pk).where(Order.user_id == self.user_id))
        runner = SyncRunner.__new__(SyncRunner)
        runner.session, runner.user_id = self.db, self.user_id
        runner.platform, runner.platform_id = self.platform, self.platform_id
        runner.client = MagicMock()
        runner.client.get_order_history.return_value = {"items": [{
            "order": {"id": "trade-1", "ticker": self.ticker, "side": "BUY", "status": "FILLED", "filledQuantity": 10},
            "fill": {"price": 100, "quantity": 10, "filledAt": "2026-01-01T12:00:00Z"}}]}
        runner.sync_order_history()
        with Session(self.engine) as reader:
            orders = reader.scalars(select(Order).where(Order.user_id == self.user_id)).all()
            self.assertEqual([o.pk for o in orders], [pk])
            self.assertEqual(orders[0].settlement_amount, 800)

    def test_unassigned_legacy_identifier_blocks_import_without_copying_or_reassigning(self):
        from app.models import Order
        self.db.add(Order(id="trade-1", user_id=self.user_id, platform_id=None, ticker=self.ticker,
                          fill_price=123, side="BUY"))
        self.db.commit()
        result = T212CsvImporter(self.db, self.platform, self.user_id).import_file(
            self.trade_csv([("Market buy", 10, 100, 800, 0)]))
        self.assertTrue(result.errors)
        from app.sync.runner import SyncRunner
        from unittest.mock import MagicMock
        runner = SyncRunner.__new__(SyncRunner)
        runner.session, runner.user_id = self.db, self.user_id
        runner.platform, runner.platform_id = self.platform, self.platform_id
        runner.client = MagicMock()
        runner.client.get_order_history.return_value = {"items": [{
            "order": {"id": "trade-1", "ticker": self.ticker, "side": "BUY", "status": "FILLED"},
            "fill": {"price": 100, "quantity": 10, "filledAt": "2026-01-01T12:00:00Z"}}]}
        with self.assertRaisesRegex(ValueError, "unresolved account or source"):
            runner._run_step(runner.sync_order_history, fatal=False)
        with Session(self.engine) as reader:
            rows = reader.scalars(select(Order).where(Order.user_id == self.user_id)).all()
            self.assertEqual(len(rows), 1)
            self.assertEqual((rows[0].platform_id, rows[0].fill_price), (None, 123))

    def test_api_transaction_cursor_is_account_and_source_scoped_and_records_owner(self):
        from app.sync.runner import SyncRunner
        from unittest.mock import MagicMock
        other = Platform(user_id=self.user_id, name="Other", platform_type="t212_api", account_type="ISA", currency="GBP")
        self.db.add(other)
        self.db.flush()
        self.db.add(Transaction(id="future", user_id=self.user_id, platform_id=other.id,
                                source_namespace="t212", date_time=datetime.datetime(2099, 1, 1)))
        self.db.add(Transaction(id="other-source", user_id=self.user_id, platform_id=self.platform_id,
                                source_namespace="ii", date_time=datetime.datetime(2099, 1, 1)))
        self.db.commit()
        runner = SyncRunner.__new__(SyncRunner)
        runner.session, runner.user_id = self.db, self.user_id
        runner.platform, runner.platform_id = self.platform, self.platform_id
        runner.client = MagicMock()
        runner.client.get_transactions.return_value = {"items": [{"reference": "new", "amount": 5, "dateTime": "2026-01-01T12:00:00Z"}]}
        runner.sync_transactions()
        self.assertIsNone(runner.client.get_transactions.call_args.kwargs["newer_than"])
        with Session(self.engine) as reader:
            new = reader.scalar(select(Transaction).where(Transaction.user_id == self.user_id, Transaction.id == "new"))
            self.assertEqual((new.platform_id, new.source_namespace), (self.platform_id, "t212"))

    def test_api_and_csv_pie_numeric_ids_cannot_collide(self):
        import zlib
        pie_id = zlib.crc32(f"csv:{self.platform_id}:CSV".encode()) & 0x7FFFFFFF
        api = Pie(id=pie_id, user_id=self.user_id, platform_id=self.platform_id, name="API", source_namespace="t212")
        self.db.add(api)
        self.db.commit()
        api_pk = api.pk
        for _ in range(2):
            result = T212PieCsvImporter(self.db, self.platform, self.user_id, "CSV").import_file(
                io.StringIO(f"Slice,Quantity\n{self.ticker},2\n"))
            self.assertEqual(result.errors, [])
        with Session(self.engine) as reader:
            pies = reader.scalars(select(Pie).where(Pie.user_id == self.user_id)).all()
            self.assertEqual(len(pies), 2)
            self.assertEqual(reader.get(Pie, api_pk).name, "API")
            self.assertEqual({p.id for p in pies}, {pie_id})

    def test_api_pies_with_same_external_id_keep_separate_holdings(self):
        from app.sync.runner import SyncRunner
        from unittest.mock import MagicMock
        other = Platform(user_id=self.user_id, name="Other", platform_type="t212_api", account_type="ISA", currency="GBP")
        self.db.add(other)
        self.db.commit()
        for platform, quantity in ((self.platform, 2), (other, 3)):
            runner = SyncRunner.__new__(SyncRunner)
            runner.session, runner.user_id = self.db, self.user_id
            runner.platform, runner.platform_id = platform, platform.id
            runner.client = MagicMock()
            runner.client.get_pies.return_value = [{"id": 7}]
            runner.client.get_pie.return_value = {"settings": {"name": "API"},
                                                "instruments": [{"ticker": self.ticker, "ownedQuantity": quantity}]}
            runner.sync_pies()
        with Session(self.engine) as reader:
            pies = reader.scalars(select(Pie).where(Pie.user_id == self.user_id)).all()
            self.assertEqual(len(pies), 2)
            self.assertEqual(sorted(p.holdings[0].owned_quantity for p in pies), [2, 3])

    def test_unassigned_identity_audit_is_read_only_and_contains_no_raw_identifiers(self):
        from scripts.audit_identity import audit_identity
        self.db.add(Transaction(id="private-reference", user_id=self.user_id, platform_id=None, amount=999))
        self.db.commit()
        with self.engine.connect() as connection:
            report = audit_identity(connection)
        self.assertEqual(report["transactions"]["unassigned_account"], 1)
        self.assertEqual(report["transactions"]["ownership_conflicts"], 0)
        self.assertNotIn("private-reference", str(report))
        with Session(self.engine) as reader:
            row = reader.scalar(select(Transaction).where(Transaction.user_id == self.user_id))
            self.assertEqual((row.platform_id, row.amount), (None, 999))

    def analysis_request(self):
        from starlette.requests import Request
        return Request({"type": "http", "method": "GET", "path": "/analysis",
                        "headers": [], "query_string": b"", "session": {"user_id": self.user_id}})

    def test_analysis_keeps_unpriced_holding_visible_and_does_not_claim_a_full_total(self):
        from app.web import server
        self.db.add(Position(user_id=self.user_id, platform_id=self.platform_id, ticker=self.ticker,
                             quantity=2, average_price=12, current_price=None))
        priced_ticker = self.ticker + "P"
        self.extra_instruments = [priced_ticker]
        self.db.add(Instrument(ticker=priced_ticker, name="Priced holding", currency_code="GBP"))
        self.db.flush()
        self.db.add(Position(user_id=self.user_id, platform_id=self.platform_id, ticker=priced_ticker,
                             quantity=3, current_price=10))
        self.db.commit()
        response = server.portfolio_analysis(self.analysis_request(), db=self.db)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([p["ticker"] for p in response.context["unpriced_positions"]], [self.ticker])
        self.assertEqual(response.context["priced_subtotal_gbp"], 30)
        self.assertIsNone(response.context["total_value"])
        self.assertIsNone(response.context["analysis_summary"]["value"])
        self.assertIsNone(response.context["analysis_summary"]["unrealised"])
        self.assertIn(b"Priced holdings subtotal", response.body)
        self.assertIn(self.ticker.encode(), response.body)
        portfolio = server.index(self.analysis_request(), self.db)
        self.assertEqual(portfolio.status_code, 200)
        self.assertIsNone(portfolio.context["total_value"])
        self.assertEqual(portfolio.context["priced_subtotal"], 30)
        self.assertEqual(portfolio.context["unpriced_count"], 1)
        with Session(self.engine) as reader:
            original = reader.get(Position, (self.ticker, self.platform_id, self.user_id))
            self.assertEqual((original.quantity, original.average_price, original.current_price), (2, 12, None))

    def test_analysis_renders_when_all_prices_or_required_fx_are_unavailable(self):
        from app.web import server
        self.db.add(Position(user_id=self.user_id, platform_id=self.platform_id, ticker=self.ticker,
                             quantity=2, average_price=12, current_price=None,
                             cost_basis_amount=24, cost_basis_currency="GBP", cost_basis_status="reported"))
        self.db.commit()
        response = server.portfolio_analysis(self.analysis_request(), db=self.db)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["positions"], [])
        self.assertIsNone(response.context["analysis_summary"]["value"])
        position = self.db.get(Position, (self.ticker, self.platform_id, self.user_id))
        position.current_price = 10
        self.db.get(Instrument, self.ticker).currency_code = "USD"
        self.db.commit()
        with patch.object(server, "_get_fx_rates_to_gbp", return_value={"GBP": 1}):
            response = server.portfolio_analysis(self.analysis_request(), db=self.db)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Exchange rate unavailable", response.context["unpriced_positions"][0]["valuation_issue"])
        with patch.object(server, "_get_fx_rates_to_gbp", return_value={"GBP": 1}):
            portfolio = server.index(self.analysis_request(), self.db)
        self.assertEqual(portfolio.status_code, 200)
        self.assertIsNone(portfolio.context["total_value"])

    def test_unpriced_holding_outside_filter_does_not_block_complete_account_view(self):
        from app.web import server
        other = Platform(user_id=self.user_id, name="Other", platform_type="t212_csv", account_type="ISA", currency="GBP")
        self.db.add(other)
        self.db.flush()
        for platform_id, price in ((self.platform_id, 10), (other.id, None)):
            self.db.add(Position(user_id=self.user_id, platform_id=platform_id, ticker=self.ticker,
                                 quantity=2, current_price=price))
        self.db.commit()
        response = server.portfolio_analysis(self.analysis_request(), account=f"platform_{self.platform_id}", db=self.db)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["analysis_summary"]["value"], 20)
        self.assertTrue(response.context["valuation_complete"])
        self.assertIsNone(response.context["total_portfolio_pct"])
