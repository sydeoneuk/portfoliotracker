import datetime
import time
from sqlalchemy.orm import Session
from sqlalchemy.dialects.postgresql import insert

from app.client.trading212 import Trading212Client
from app.models import Instrument, Pie, PieHolding, Position, Order, Transaction, DividendPayment


class SyncRunner:
    def __init__(self, session: Session, account: str = "Trading",
                 api_key: str | None = None, api_secret: str | None = None,
                 user_id: int | None = None):
        self.session = session
        self.account = account
        self.user_id = user_id
        self.client = Trading212Client(api_key=api_key, api_secret=api_secret)

    def sync_all(self):
        print(f"Starting full sync for account: {self.account}...")
        self.sync_instruments()
        self.sync_positions()
        self.sync_pies()
        self.sync_open_orders()
        try:
            self.sync_order_history()
        except Exception as exc:
            print(f"  Order history sync failed (non-fatal): {exc}")
        self.sync_transactions()
        self.sync_dividend_payments()
        print("Sync complete.")

    def sync_instruments(self):
        # Instruments change rarely — skip if synced within the last 24 hours.
        from sqlalchemy import text as _text
        last = self.session.execute(
            _text("SELECT MAX(updated_at) FROM instruments")
        ).scalar()
        if last and (datetime.datetime.utcnow() - last).total_seconds() < 86400:
            print("  Instruments up to date (synced < 24h ago), skipping.")
            return
        print("Syncing instruments...")
        data = self.client.get_instruments()
        now = datetime.datetime.utcnow()

        for item in data:
            stmt = (
                insert(Instrument)
                .values(
                    ticker=item["ticker"],
                    name=item.get("name"),
                    short_name=item.get("shortName"),
                    currency_code=item.get("currencyCode"),
                    isin=item.get("isin"),
                    instrument_type=item.get("type"),
                    exchange=item.get("exchange"),
                    min_trade_quantity=item.get("minTradeQuantity"),
                    max_open_quantity=item.get("maxOpenQuantity"),
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_update(
                    index_elements=["ticker"],
                    set_={
                        "name": item.get("name"),
                        "short_name": item.get("shortName"),
                        "currency_code": item.get("currencyCode"),
                        "isin": item.get("isin"),
                        "exchange": item.get("exchange"),
                        "updated_at": now,
                    },
                )
            )
            self.session.execute(stmt)

        self.session.commit()
        print(f"  Synced {len(data)} instruments.")

    def sync_positions(self):
        print("Syncing positions...")
        data = self.client.get_positions()
        now = datetime.datetime.utcnow()

        live_tickers = {item["ticker"] for item in data}
        q = self.session.query(Position.ticker).filter(Position.account == self.account)
        if self.user_id is not None:
            q = q.filter(Position.user_id == self.user_id)
        stored_tickers = {r[0] for r in q.all()}

        closed = stored_tickers - live_tickers
        if closed:
            dq = self.session.query(Position).filter(
                Position.ticker.in_(closed), Position.account == self.account
            )
            if self.user_id is not None:
                dq = dq.filter(Position.user_id == self.user_id)
            dq.delete()
            print(f"  Removed {len(closed)} closed positions.")

        for item in data:
            stmt = (
                insert(Position)
                .values(
                    ticker=item["ticker"],
                    account=self.account,
                    user_id=self.user_id,
                    quantity=item.get("quantity"),
                    average_price=item.get("averagePrice"),
                    current_price=item.get("currentPrice"),
                    ppl=item.get("ppl"),
                    fx_ppl=item.get("fxPpl"),
                    result_coef=item.get("resultCoef"),
                    last_synced_at=now,
                )
                .on_conflict_do_update(
                    index_elements=["ticker", "account", "user_id"],
                    set_={
                        "quantity": item.get("quantity"),
                        "average_price": item.get("averagePrice"),
                        "current_price": item.get("currentPrice"),
                        "ppl": item.get("ppl"),
                        "fx_ppl": item.get("fxPpl"),
                        "result_coef": item.get("resultCoef"),
                        "last_synced_at": now,
                    },
                )
            )
            self.session.execute(stmt)

        self.session.commit()
        print(f"  Synced {len(data)} positions.")

    def sync_pies(self):
        # Pies change rarely (only when user manually rebalances) — skip if synced within 4 hours.
        from sqlalchemy import text as _text
        latest_pie = self.session.execute(
            _text(
                "SELECT MAX(last_synced_at) FROM pies "
                "WHERE account = :acc OR account IS NULL"
            ),
            {"acc": self.account},
        ).scalar()
        if latest_pie and (datetime.datetime.utcnow() - latest_pie).total_seconds() < 14400:
            print("  Pies up to date (synced < 4h ago), skipping.")
            return

        print("Syncing pies...")
        pie_list = self.client.get_pies()
        now = datetime.datetime.utcnow()

        for summary in pie_list:
            pie_id = summary["id"]
            detail = self.client.get_pie(pie_id)
            s = detail.get("settings", {})
            instruments = detail.get("instruments", [])

            creation_ts = s.get("creationDate")
            creation_date = (
                datetime.datetime.utcfromtimestamp(creation_ts) if creation_ts else None
            )
            end_date_str = s.get("endDate")
            end_date = (
                datetime.datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                if end_date_str
                else None
            )

            stmt = (
                insert(Pie)
                .values(
                    id=pie_id,
                    name=s.get("name"),
                    account=self.account,
                    icon=s.get("icon"),
                    goal=s.get("goal"),
                    creation_date=creation_date,
                    end_date=end_date,
                    initial_investment=s.get("initialInvestment"),
                    dividend_cash_action=s.get("dividendCashAction"),
                    last_synced_at=now,
                    created_at=now,
                )
                .on_conflict_do_update(
                    index_elements=["id"],
                    set_={
                        "name": s.get("name"),
                        "account": self.account,
                        "goal": s.get("goal"),
                        "end_date": end_date,
                        "last_synced_at": now,
                    },
                )
            )
            self.session.execute(stmt)
            self.session.flush()

            # Ensure every ticker referenced by this pie exists in instruments.
            # T212's metadata endpoint is region-scoped so some tickers (e.g. TSX stocks)
            # may be absent. Insert minimal stubs so the FK constraint is satisfied.
            pie_tickers = {item["ticker"] for item in instruments}
            existing = {
                r[0]
                for r in self.session.query(Instrument.ticker)
                .filter(Instrument.ticker.in_(pie_tickers))
                .all()
            }
            for missing_ticker in pie_tickers - existing:
                stub_stmt = (
                    insert(Instrument)
                    .values(ticker=missing_ticker, created_at=now, updated_at=now)
                    .on_conflict_do_nothing(index_elements=["ticker"])
                )
                self.session.execute(stub_stmt)
            if pie_tickers - existing:
                print(f"  Inserted {len(pie_tickers - existing)} instrument stub(s) for out-of-region tickers.")

            # Replace all holdings for this pie
            self.session.query(PieHolding).filter_by(pie_id=pie_id).delete()
            for item in instruments:
                result = item.get("result", {})
                holding = PieHolding(
                    pie_id=pie_id,
                    ticker=item["ticker"],
                    expected_share=item.get("expectedShare"),
                    current_share=item.get("currentShare"),
                    owned_quantity=item.get("ownedQuantity"),
                    price_avg_invested_value=result.get("priceAvgInvestedValue"),
                    price_avg_value=result.get("priceAvgValue"),
                    price_avg_result=result.get("priceAvgResult"),
                    price_avg_result_coef=result.get("priceAvgResultCoef"),
                    synced_at=now,
                )
                self.session.add(holding)

        self.session.commit()
        print(f"  Synced {len(pie_list)} pies.")

    def sync_open_orders(self):
        print("Syncing open orders...")
        data = self.client.get_open_orders()
        now = datetime.datetime.utcnow()

        self.session.query(Order).filter(Order.status == "LOCAL_OPEN").delete()

        for item in data:
            stmt = (
                insert(Order)
                .values(
                    id=str(item["id"]),
                    ticker=item.get("ticker"),
                    quantity=item.get("quantity"),
                    filled_quantity=item.get("filledQuantity"),
                    order_type=item.get("type"),
                    status=item.get("status", "LOCAL_OPEN"),
                    limit_price=item.get("limitPrice"),
                    stop_price=item.get("stopPrice"),
                    fill_price=item.get("fillPrice"),
                    time_validity=item.get("timeValidity"),
                    created_at=_parse_dt(item.get("creationTime")),
                    filled_at=_parse_dt(item.get("dateModified")),
                    synced_at=now,
                )
                .on_conflict_do_update(
                    index_elements=["id"],
                    set_={
                        "status": item.get("status", "LOCAL_OPEN"),
                        "filled_quantity": item.get("filledQuantity"),
                        "fill_price": item.get("fillPrice"),
                        "synced_at": now,
                    },
                )
            )
            self.session.execute(stmt)

        self.session.commit()
        print(f"  Synced {len(data)} open orders.")

    def sync_order_history(self):
        """Syncs historical filled/cancelled orders for this account.

        On first run fetches all history. On subsequent runs only fetches orders
        newer than the most recently stored filled_at for this user+account.
        """
        print("Syncing order history...")
        now = datetime.datetime.utcnow()
        next_page = None
        total = 0

        # Use the most recent filled_at as an incremental cursor.
        # On first sync, cap lookback to 12 months to avoid paginating years of history.
        from sqlalchemy import text as _text
        latest = self.session.execute(
            _text("SELECT MAX(filled_at) FROM orders WHERE user_id = :uid AND account = :acc"),
            {"uid": self.user_id, "acc": self.account},
        ).scalar()
        if latest:
            newer_than = latest.strftime("%Y-%m-%dT%H:%M:%S")
            print(f"  Fetching orders newer than {newer_than}")
        else:
            cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=365)
            newer_than = cutoff.strftime("%Y-%m-%dT%H:%M:%S")
            print(f"  First sync — fetching orders from the last 12 months ({newer_than})")

        page = 0
        while True:
            page += 1
            if page > 500:
                print("  WARNING: order history pagination safety limit reached, stopping.")
                break
            try:
                response = self.client.get_order_history(
                    next_page=next_page, newer_than=newer_than if not next_page else None
                )
            except Exception as exc:
                print(f"  Stopping order history pagination: {exc}")
                break

            items = response.get("items", []) if isinstance(response, dict) else response
            if not items:
                break

            for item in items:
                # T212 history orders API wraps data in nested "order" and "fill" objects
                order = item.get("order", {})
                fill = item.get("fill", {})
                order_id = order.get("id")
                if not order_id:
                    continue
                ticker = order.get("ticker") or (order.get("instrument") or {}).get("ticker")
                fill_price = fill.get("price") or None  # 0 means no price data
                stmt = (
                    insert(Order)
                    .values(
                        id=str(order_id),
                        ticker=ticker,
                        user_id=self.user_id,
                        account=self.account,
                        quantity=order.get("quantity"),
                        filled_quantity=order.get("filledQuantity") or fill.get("quantity"),
                        order_type=order.get("type"),
                        side=order.get("side"),
                        status=order.get("status"),
                        limit_price=order.get("limitPrice"),
                        stop_price=order.get("stopPrice"),
                        fill_price=fill_price,
                        time_validity=order.get("timeValidity"),
                        created_at=_parse_dt(order.get("createdAt")),
                        filled_at=_parse_dt(fill.get("filledAt")),
                        synced_at=now,
                    )
                    .on_conflict_do_update(
                        index_elements=["id"],
                        set_={
                            "status": order.get("status"),
                            "filled_quantity": order.get("filledQuantity") or fill.get("quantity"),
                            "fill_price": fill_price,
                            "filled_at": _parse_dt(fill.get("filledAt")),
                            "side": order.get("side"),
                            "user_id": self.user_id,
                            "account": self.account,
                            "synced_at": now,
                        },
                    )
                )
                self.session.execute(stmt)

            total += len(items)
            next_page = response.get("nextPagePath")
            print(f"  Order history page {page}: {len(items)} items (total so far: {total}), more={bool(next_page)}")
            if not next_page:
                break

        self.session.commit()
        print(f"  Synced {total} historical orders.")

    def sync_transactions(self):
        """Syncs transactions, fetching only those newer than the most recent stored record."""
        print("Syncing transactions...")
        now = datetime.datetime.utcnow()
        next_page = None
        total = 0

        from sqlalchemy import text as _text
        latest = self.session.execute(
            _text("SELECT MAX(date_time) FROM transactions")
        ).scalar()
        # Subtract 60s buffer to avoid missing records on timestamp boundaries
        if latest:
            cursor_dt = latest - datetime.timedelta(seconds=60)
            newer_than = cursor_dt.strftime("%Y-%m-%dT%H:%M:%S")
            print(f"  Fetching transactions newer than {newer_than}")
        else:
            newer_than = None

        page = 0
        while True:
            page += 1
            if page > 500:
                print("  WARNING: transaction pagination safety limit reached, stopping.")
                break
            try:
                response = self.client.get_transactions(
                    next_page=next_page, newer_than=newer_than if not next_page else None
                )
            except Exception as exc:
                print(f"  Stopping transaction pagination: {exc}")
                break

            items = response.get("items", [])
            if not items:
                break

            for item in items:
                stmt = (
                    insert(Transaction)
                    .values(
                        id=str(item.get("reference", item.get("id", ""))),
                        type=item.get("type"),
                        amount=item.get("amount"),
                        date_time=_parse_dt(item.get("dateTime")),
                        reference=item.get("reference"),
                        notes=item.get("notes"),
                        synced_at=now,
                    )
                    .on_conflict_do_nothing(index_elements=["id"])
                )
                self.session.execute(stmt)

            total += len(items)
            next_page = response.get("nextPagePath")
            print(f"  Transactions page {page}: {len(items)} items (total so far: {total}), more={bool(next_page)}")
            if not next_page:
                break

        self.session.commit()
        print(f"  Synced {total} transactions.")


    def sync_dividend_payments(self):
        """Fetch dividend payments newer than the most recently stored record for this account."""
        print("Syncing dividend payments...")
        now = datetime.datetime.utcnow()
        next_page = None
        total = 0

        from sqlalchemy import text as _text
        latest = self.session.execute(
            _text(
                "SELECT MAX(paid_on) FROM dividend_payments "
                "WHERE user_id = :uid AND account = :acc"
            ),
            {"uid": self.user_id, "acc": self.account},
        ).scalar()
        if latest:
            cursor_dt = latest - datetime.timedelta(seconds=60)
            newer_than = cursor_dt.strftime("%Y-%m-%dT%H:%M:%S")
            print(f"  Fetching dividend payments newer than {newer_than}")
        else:
            newer_than = None

        page = 0
        while True:
            page += 1
            if page > 500:
                print("  WARNING: dividend payment pagination safety limit reached, stopping.")
                break
            try:
                response = self.client.get_dividend_payments(
                    next_page=next_page, newer_than=newer_than if not next_page else None
                )
            except Exception as exc:
                print(f"  Stopping dividend payment pagination: {exc}")
                break

            items = response.get("items", [])
            if not items:
                break

            # Ensure all referenced tickers exist as instruments
            batch_tickers = {
                item.get("ticker") or (item.get("instrument") or {}).get("ticker")
                for item in items
            } - {None}
            existing = {
                r[0] for r in self.session.query(Instrument.ticker)
                .filter(Instrument.ticker.in_(batch_tickers)).all()
            }
            for missing_ticker in batch_tickers - existing:
                self.session.execute(
                    insert(Instrument)
                    .values(ticker=missing_ticker, created_at=now, updated_at=now)
                    .on_conflict_do_nothing(index_elements=["ticker"])
                )

            for item in items:
                ticker = item.get("ticker") or (item.get("instrument") or {}).get("ticker")
                if not ticker:
                    continue
                stmt = (
                    insert(DividendPayment)
                    .values(
                        reference=item["reference"],
                        ticker=ticker,
                        account=self.account,
                        user_id=self.user_id,
                        quantity=item.get("quantity"),
                        amount=item.get("amount"),
                        gross_amount_per_share=item.get("grossAmountPerShare"),
                        paid_on=_parse_dt(item.get("paidOn")),
                        type=item.get("type"),
                        synced_at=now,
                    )
                    .on_conflict_do_update(
                        index_elements=["reference"],
                        set_={"user_id": self.user_id, "synced_at": now},
                    )
                )
                self.session.execute(stmt)

            total += len(items)
            next_page = response.get("nextPagePath")
            print(f"  Dividend payments page {page}: {len(items)} items (total so far: {total}), more={bool(next_page)}")
            if not next_page:
                break

        self.session.commit()
        print(f"  Synced {total} dividend payments.")


def _parse_dt(value: str | None) -> datetime.datetime | None:
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
