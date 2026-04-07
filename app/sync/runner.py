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

    def _run_step(self, fn, fatal: bool = True):
        """Run a single sync step, rolling back the session on failure.

        If fatal=True (default) the exception is re-raised after rollback so
        the whole sync aborts. If fatal=False the error is logged and skipped.
        """
        try:
            fn()
        except Exception as exc:
            self.session.rollback()
            print(f"  {fn.__name__} failed: {exc}")
            if fatal:
                raise

    def sync_all(self):
        print(f"Starting full sync for account: {self.account}...")
        # Positions and pies first so we know which tickers are held before
        # fetching the T212 instrument catalogue (avoids storing all 16k+ rows).
        self._run_step(self.sync_positions)
        self._run_step(self.sync_pies)
        self._run_step(self.sync_account_cash, fatal=False)
        self._run_step(self.sync_instruments)
        self._run_step(self.sync_open_orders)
        self._run_step(self.sync_order_history, fatal=False)
        self._run_step(self.sync_transactions)
        self._run_step(self.sync_dividend_payments)
        self._run_step(self.sync_instrument_metadata, fatal=False)
        print("Sync complete.")

    def sync_instruments(self):
        """Sync T212 instrument metadata for held tickers only.

        Fetches the full T212 catalogue (one API call) but only upserts rows for
        tickers the user currently holds in positions or pies. Skips if all held
        instruments were updated within the last 24 hours.
        """
        from sqlalchemy import text as _text

        # Collect tickers the user holds (positions already synced at this point)
        held_tickers = {
            r[0] for r in self.session.query(Position.ticker)
            .filter(Position.user_id == self.user_id).all()
        } | {
            r[0] for r in self.session.query(PieHolding.ticker)
            .filter(PieHolding.user_id == self.user_id).all()
        }

        if not held_tickers:
            print("  No held positions found, skipping instrument sync.")
            return

        # Skip if all held instruments were refreshed within the last 24 hours
        last = self.session.execute(
            _text("SELECT MIN(updated_at) FROM instruments WHERE ticker = ANY(:tickers)"),
            {"tickers": list(held_tickers)},
        ).scalar()
        if last and (datetime.datetime.utcnow() - last).total_seconds() < 86400:
            print("  Instruments up to date (synced < 24h ago), skipping.")
            return

        print(f"Syncing instruments for {len(held_tickers)} held tickers...")
        data = self.client.get_instruments()
        now = datetime.datetime.utcnow()

        # Filter catalogue to held tickers only
        relevant = [item for item in data if item["ticker"] in held_tickers]

        for item in relevant:
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
        print(f"  Synced {len(relevant)} instruments (filtered from {len(data)} in catalogue).")

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

        # Ensure all position tickers exist in instruments before inserting.
        # Positions may reference tickers not yet in the catalogue (e.g. out-of-region
        # instruments like EEIl_EQ). Insert minimal stubs so the FK constraint is satisfied;
        # sync_instruments will enrich them afterwards.
        position_tickers = {item["ticker"] for item in data}
        existing_tickers = {
            r[0] for r in self.session.query(Instrument.ticker)
            .filter(Instrument.ticker.in_(position_tickers)).all()
        }
        for missing in position_tickers - existing_tickers:
            self.session.execute(
                insert(Instrument)
                .values(ticker=missing, created_at=now, updated_at=now)
                .on_conflict_do_nothing(index_elements=["ticker"])
            )
        if position_tickers - existing_tickers:
            self.session.flush()
            print(f"  Inserted {len(position_tickers - existing_tickers)} instrument stub(s) for positions.")

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
                "WHERE user_id = :uid AND (account = :acc OR account IS NULL)"
            ),
            {"uid": self.user_id, "acc": self.account},
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

            result_data = detail.get("result", {})
            pie_cash = result_data.get("cash")

            stmt = (
                insert(Pie)
                .values(
                    id=pie_id,
                    user_id=self.user_id,
                    name=s.get("name"),
                    account=self.account,
                    icon=s.get("icon"),
                    goal=s.get("goal"),
                    creation_date=creation_date,
                    end_date=end_date,
                    initial_investment=s.get("initialInvestment"),
                    dividend_cash_action=s.get("dividendCashAction"),
                    cash=pie_cash,
                    last_synced_at=now,
                    created_at=now,
                )
                .on_conflict_do_update(
                    # Unique constraint is (user_id, id) — each user owns their own pie row
                    index_elements=["user_id", "id"],
                    set_={
                        "name": s.get("name"),
                        "account": self.account,
                        "goal": s.get("goal"),
                        "end_date": end_date,
                        "cash": pie_cash,
                        "last_synced_at": now,
                    },
                )
                .returning(Pie.pk)
            )
            # Get the surrogate PK back — pie_holdings.pie_id references pies.pk, not pies.id
            surrogate_pie_pk = self.session.execute(stmt).scalar()
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

            # Replace all holdings for this user+pie using the surrogate PK
            self.session.query(PieHolding).filter_by(
                user_id=self.user_id, pie_id=surrogate_pie_pk
            ).delete()
            for item in instruments:
                result = item.get("result", {})
                holding = PieHolding(
                    user_id=self.user_id,
                    pie_id=surrogate_pie_pk,
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

    def sync_account_cash(self):
        """Fetch current cash position and store on UserSettings.

        T212 returns either a flat structure {free, pieCash, ...} or a nested
        structure {cash: {free, inPies, ...}}. Both shapes are handled.
        """
        print("Syncing account cash...")
        try:
            data = self.client.get_account_cash()
        except Exception as exc:
            print(f"  Account cash sync failed (non-fatal): {exc}")
            return

        print(f"  Account cash response: {data}")

        # Handle nested {cash: {free, inPies}} and flat {free, pieCash} shapes
        cash_block = data.get("cash", data)
        free = cash_block.get("free") or data.get("free")
        pie_cash = cash_block.get("inPies") or data.get("pieCash")

        if self.user_id is None:
            return

        from app.auth.models import UserSettings
        settings_row = self.session.query(UserSettings).filter_by(user_id=self.user_id).first()
        if settings_row is None:
            return

        if self.account == "ISA":
            settings_row.free_cash_isa = free
            settings_row.pie_cash_isa = pie_cash
        else:
            settings_row.free_cash_trading = free
            settings_row.pie_cash_trading = pie_cash

        self.session.commit()
        print(f"  Stored cash for {self.account}: free={free}, in_pies={pie_cash}")

    def sync_open_orders(self):
        print("Syncing open orders...")
        data = self.client.get_open_orders()
        now = datetime.datetime.utcnow()

        self.session.query(Order).filter(
            Order.status == "LOCAL_OPEN",
            Order.user_id == self.user_id,
        ).delete()

        for item in data:
            stmt = (
                insert(Order)
                .values(
                    id=str(item["id"]),
                    user_id=self.user_id,
                    account=self.account,
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
                    index_elements=["user_id", "id"],
                    set_={
                        "account": self.account,
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
            newer_than = latest.strftime("%Y-%m-%dT%H:%M:%S.000+00:00")
            print(f"  Fetching orders newer than {newer_than}")
        else:
            cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=365)
            newer_than = cutoff.strftime("%Y-%m-%dT%H:%M:%S.000+00:00")
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
                        index_elements=["user_id", "id"],
                        set_={
                            "status": order.get("status"),
                            "filled_quantity": order.get("filledQuantity") or fill.get("quantity"),
                            "fill_price": fill_price,
                            "filled_at": _parse_dt(fill.get("filledAt")),
                            "side": order.get("side"),
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
            _text("SELECT MAX(date_time) FROM transactions WHERE user_id = :uid"),
            {"uid": self.user_id},
        ).scalar()
        # Subtract 60s buffer to avoid missing records on timestamp boundaries
        if latest:
            cursor_dt = latest - datetime.timedelta(seconds=60)
            newer_than = cursor_dt.strftime("%Y-%m-%dT%H:%M:%S.000+00:00")
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

            if page == 1:
                import json as _json
                print("  Sample transaction items:", _json.dumps(items[:3], indent=2, default=str))

            for item in items:
                stmt = (
                    insert(Transaction)
                    .values(
                        id=str(item.get("reference", item.get("id", ""))),
                        user_id=self.user_id,
                        type=item.get("type"),
                        amount=item.get("amount"),
                        date_time=_parse_dt(item.get("dateTime")),
                        reference=item.get("reference"),
                        notes=item.get("notes"),
                        synced_at=now,
                    )
                    .on_conflict_do_update(
                        index_elements=["user_id", "id"],
                        set_={"synced_at": now},
                    )
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
            newer_than = cursor_dt.strftime("%Y-%m-%dT%H:%M:%S.000+00:00")
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
                        index_elements=["user_id", "reference"],
                        set_={"synced_at": now},
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

    def sync_instrument_metadata(self):
        """Enrich held instruments with metadata from yfinance (primary) and FMP (fallback).

        Fetches description, sector, industry and country. Only processes instruments
        where last_enriched_at IS NULL or older than 7 days so subsequent syncs are fast.
        """
        import yfinance as yf
        from app.config import settings as _settings
        from app.enrichment.ticker_mapper import derive_yf_ticker, EXCHANGE_COUNTRY_MAP
        from app.enrichment.fmp_enricher import FmpDividendEnricher
        from app.enrichment.claude_enricher import ClaudeDescriptionEnricher

        fmp = FmpDividendEnricher(_settings.fmp_api_key) if _settings.fmp_api_key else None
        claude = ClaudeDescriptionEnricher(_settings.anthropic_api_key) if _settings.anthropic_api_key else None

        # Only enrich instruments this user currently holds
        held_tickers = {
            r[0] for r in self.session.query(Position.ticker)
            .filter(Position.user_id == self.user_id).all()
        }
        if not held_tickers:
            return

        # Back-fill country from exchange for already-enriched instruments that are missing it.
        # This fixes instruments that were enriched before the exchange fallback was added.
        backfill_instruments = (
            self.session.query(Instrument)
            .filter(Instrument.ticker.in_(held_tickers))
            .filter(Instrument.country == None)  # noqa: E711
            .filter(Instrument.exchange != None)  # noqa: E711
            .all()
        )
        backfilled = 0
        for inst in backfill_instruments:
            inferred = EXCHANGE_COUNTRY_MAP.get((inst.exchange or "").upper())
            if inferred:
                inst.country = inferred
                backfilled += 1
        if backfilled:
            self.session.commit()
            print(f"  Back-filled country from exchange for {backfilled} instrument(s).")

        cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=7)
        instruments = (
            self.session.query(Instrument)
            .filter(Instrument.ticker.in_(held_tickers))
            .filter(
                (Instrument.last_enriched_at == None) |  # noqa: E711
                (Instrument.last_enriched_at < cutoff)
            )
            .all()
        )

        if not instruments:
            print("  Instrument metadata up to date, skipping.")
            return

        print(f"Enriching metadata for {len(instruments)} instruments...")
        now = datetime.datetime.utcnow()
        enriched = 0

        for instrument in instruments:
            yf_ticker = instrument.yf_ticker or derive_yf_ticker(
                instrument.ticker, instrument.short_name, instrument.exchange
            )
            if not yf_ticker:
                continue
            try:
                info = yf.Ticker(yf_ticker).info
                if info.get("longBusinessSummary"):
                    instrument.description = info["longBusinessSummary"]
                if info.get("sector"):
                    instrument.sector = info["sector"]
                if info.get("industry"):
                    instrument.industry = info["industry"]
                if info.get("country"):
                    instrument.country = info["country"]
                elif not instrument.country:
                    # yfinance didn't return a country — infer from exchange
                    instrument.country = EXCHANGE_COUNTRY_MAP.get(
                        (instrument.exchange or "").upper()
                    )
                instrument.yf_ticker = yf_ticker
                instrument.last_enriched_at = now

                # Fall back to FMP for description if yfinance came up empty
                if not instrument.description and fmp:
                    fmp_desc = fmp.get_description(yf_ticker, instrument.instrument_type or "")
                    if fmp_desc:
                        instrument.description = fmp_desc
                        print(f"  {instrument.ticker}: description from FMP")

                # Try Claude API if still no description
                if not instrument.description and claude:
                    display_name = instrument.name or instrument.short_name or instrument.ticker
                    claude_desc = claude.get_description(display_name)
                    if claude_desc:
                        instrument.description = claude_desc
                        print(f"  {instrument.ticker}: description from Claude")

                # Last resort: synthesise a description from available metadata
                if not instrument.description:
                    instrument.description = _synthesise_description(instrument)
                    if instrument.description:
                        print(f"  {instrument.ticker}: description synthesised from metadata")

                enriched += 1
                print(f"  Enriched {instrument.ticker} ({yf_ticker})")
            except Exception as exc:
                print(f"  Failed to enrich {instrument.ticker}: {exc}")

        self.session.commit()
        print(f"  Metadata enrichment complete ({enriched}/{len(instruments)} instruments).")


def _synthesise_description(instrument) -> str | None:
    """Build a basic description from instrument metadata when no external source has one."""
    name = instrument.name or instrument.short_name
    if not name:
        return None

    parts = []

    # Instrument class (REIT, ETF, Investment Trust, BDC, etc.)
    iclass = (instrument.instrument_class or "").strip()
    itype  = (instrument.instrument_type or "").upper()

    if iclass and iclass not in ("Stock", "—"):
        parts.append(f"{name} is a {iclass}")
    elif itype == "ETF":
        parts.append(f"{name} is an Exchange-Traded Fund (ETF)")
    else:
        parts.append(f"{name} is a publicly listed company")

    # Country
    country = (instrument.country or "").strip()
    if country:
        parts[-1] += f" based in {country}"

    # Exchange
    exchange = (instrument.exchange or "").strip()
    if exchange and exchange != "—":
        parts[-1] += f", listed on {exchange}"

    parts[-1] += "."

    # Sector / industry
    sector   = (instrument.sector or "").strip()
    industry = (instrument.industry or "").strip()
    if sector and industry and sector.lower() != industry.lower():
        parts.append(f"It operates in the {industry} industry within the {sector} sector.")
    elif sector:
        parts.append(f"It operates in the {sector} sector.")
    elif industry:
        parts.append(f"It operates in the {industry} industry.")

    return " ".join(parts) if parts else None


def _parse_dt(value: str | None) -> datetime.datetime | None:
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
