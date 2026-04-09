import datetime
import time
import requests
from sqlalchemy import literal_column
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
            if self._is_permission_error(exc):
                print(f"  {fn.__name__} skipped: API token lacks permission ({exc})")
                return

            print(f"  {fn.__name__} failed: {exc}")
            if fatal:
                raise

    @staticmethod
    def _is_permission_error(exc: Exception) -> bool:
        if not isinstance(exc, requests.HTTPError):
            return False
        response = exc.response
        return response is not None and response.status_code in (401, 403)

    def _held_tickers(self) -> set[str]:
        """Return tickers held directly or inside pies for the current user."""
        position_q = self.session.query(Position.ticker)
        pie_q = self.session.query(PieHolding.ticker)

        if self.user_id is not None:
            position_q = position_q.filter(Position.user_id == self.user_id)
            pie_q = pie_q.filter(PieHolding.user_id == self.user_id)

        return {r[0] for r in position_q.all()} | {r[0] for r in pie_q.all()}

    def _resolve_yfinance_metadata(self, instrument, yf):
        """Try stored and derived Yahoo symbols until one returns usable metadata."""
        from app.enrichment.ticker_mapper import build_yf_ticker_candidates

        candidates: list[str] = []
        if instrument.yf_ticker:
            candidates.append(instrument.yf_ticker)
        for candidate in build_yf_ticker_candidates(
            instrument.ticker, instrument.short_name, instrument.exchange
        ):
            if candidate not in candidates:
                candidates.append(candidate)

        if not candidates:
            return None, None

        last_exc = None
        for candidate in candidates:
            try:
                time.sleep(0.5)
                ticker_obj = yf.Ticker(candidate)
                info = ticker_obj.info or {}
            except Exception as exc:
                last_exc = exc
                continue

            if _is_useful_yf_info(info):
                return candidate, info, ticker_obj

        if last_exc:
            raise last_exc

        raise ValueError("No usable yfinance metadata returned")

    def sync_all(self, full_catalogue: bool = False):
        print(f"Starting full sync for account: {self.account}...")
        # Positions and pies are synced first so we know which tickers are held.
        # full_catalogue=True (nightly only) additionally stores all ~16k T212
        # instruments, not just the ones held, enabling the research/filter page.
        self._run_step(self.sync_positions)
        self._run_step(self.sync_pies)
        self._run_step(self.sync_account_cash, fatal=False)
        self._run_step(lambda: self.sync_instruments(full_catalogue=full_catalogue))
        self._run_step(self.sync_open_orders)
        self._run_step(self.sync_order_history, fatal=False)
        self._run_step(self.sync_transactions)
        self._run_step(self.sync_dividend_payments)
        self._run_step(self.sync_instrument_metadata, fatal=False)
        print("Sync complete.")

    def sync_instruments(self, full_catalogue: bool = False):
        """Sync T212 instrument metadata.

        Fetches the full T212 catalogue (one API call). When full_catalogue is
        False (default, used by manual user syncs) only held tickers and any
        existing DB stubs are upserted — keeping the table lean during normal
        use. When full_catalogue is True (nightly sync) every instrument in the
        T212 catalogue is upserted, populating the instruments table for the
        investment research / filter page.

        Skips if held instruments were updated within the last 24 hours (unless
        full_catalogue is True, in which case it always runs).
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

        # Skip freshness check when loading the full catalogue
        if not full_catalogue:
            last = self.session.execute(
                _text("SELECT MIN(updated_at) FROM instruments WHERE ticker = ANY(:tickers)"),
                {"tickers": list(held_tickers)},
            ).scalar()
            if last and (datetime.datetime.utcnow() - last).total_seconds() < 86400:
                print("  Instruments up to date (synced < 24h ago), skipping.")
                return

        # Build the set of tickers to upsert
        existing_tickers = {r[0] for r in self.session.query(Instrument.ticker).all()}

        if full_catalogue:
            # Upsert everything — upsert_tickers is resolved after catalogue download
            upsert_tickers = None
            print("Syncing full T212 instrument catalogue...")
        else:
            upsert_tickers = held_tickers | existing_tickers
            stub_count = len(existing_tickers - held_tickers)
            print(f"Syncing instruments: {len(held_tickers)} held + {stub_count} existing stubs...")

        data = self.client.get_instruments()
        now = datetime.datetime.utcnow()

        from app.enrichment.ticker_mapper import EXCHANGE_CURRENCY_MAP, derive_exchange_from_ticker

        relevant = data if full_catalogue else [item for item in data if item["ticker"] in upsert_tickers]

        for item in relevant:
            # T212 does not include exchange in the API response; derive it from
            # the ticker format (e.g. AAPL_US_EQ → "US", SAP_XETR_EQ → "XETR")
            exchange = item.get("exchange") or derive_exchange_from_ticker(item["ticker"]) or ""
            # Use T212-supplied currency; fall back to exchange map if absent
            currency = item.get("currencyCode") or EXCHANGE_CURRENCY_MAP.get(exchange.upper())
            stmt = (
                insert(Instrument)
                .values(
                    ticker=item["ticker"],
                    name=item.get("name"),
                    short_name=item.get("shortName"),
                    currency_code=currency,
                    isin=item.get("isin"),
                    instrument_type=item.get("type"),
                    exchange=exchange or None,
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
                        # Never overwrite a known currency with NULL
                        "currency_code": literal_column(
                            "COALESCE(EXCLUDED.currency_code, instruments.currency_code)"
                        ),
                        "isin": item.get("isin"),
                        "instrument_type": item.get("type"),
                        # Prefer the incoming derived exchange, but never overwrite a
                        # more specific value (e.g. "NYSE" set by yfinance) with a
                        # coarser one (e.g. "US") from the ticker parse
                        "exchange": literal_column(
                            "COALESCE(EXCLUDED.exchange, instruments.exchange)"
                        ),
                        "min_trade_quantity": item.get("minTradeQuantity"),
                        "max_open_quantity": item.get("maxOpenQuantity"),
                        "updated_at": now,
                    },
                )
            )
            self.session.execute(stmt)

        # Back-fill NULL currency for all matching instruments using the exchange map.
        # When full_catalogue=True, upsert_tickers is None so we skip the ticker
        # filter and back-fill across the whole table.
        backfill_q = self.session.query(Instrument).filter(
            Instrument.currency_code.is_(None),
            Instrument.exchange.isnot(None),
        )
        if not full_catalogue:
            backfill_q = backfill_q.filter(Instrument.ticker.in_(upsert_tickers))
        backfill = backfill_q.all()
        for inst in backfill:
            derived = EXCHANGE_CURRENCY_MAP.get((inst.exchange or "").upper())
            if derived:
                inst.currency_code = derived
                print(f"  Back-filled currency {derived} for {inst.ticker} (exchange={inst.exchange})")

        self.session.commit()
        label = "full catalogue" if full_catalogue else "held + stubs"
        print(f"  Synced {len(relevant)} instruments ({label}) from T212 catalogue ({len(data)} total).")

    def reload_all_instruments_from_t212(self, progress_callback=None) -> dict:
        """Force-reload T212 base data for every instrument currently in the DB.

        Downloads the T212 instruments catalogue once and upserts all rows that
        match an instrument already in our database. Does NOT add new instruments
        (sync_instruments handles that). Bypasses the 24h freshness check.

        Instruments not present in the T212 catalogue (delisted, stubs for
        non-T212 assets) are left as-is; their currency is back-filled from
        the exchange map if still NULL.

        Args:
            progress_callback: optional callable(done, total, ticker)

        Returns:
            dict with keys: total_in_db, found_in_catalogue,
                            not_in_catalogue, backfilled_currency
        """
        from app.enrichment.ticker_mapper import EXCHANGE_CURRENCY_MAP, derive_exchange_from_ticker

        existing_tickers = {r[0] for r in self.session.query(Instrument.ticker).all()}
        total = len(existing_tickers)

        print(f"[t212-reload] Fetching T212 catalogue to refresh {total} instruments...")
        data = self.client.get_instruments()
        now = datetime.datetime.utcnow()

        relevant = [item for item in data if item["ticker"] in existing_tickers]
        not_in_catalogue = len(existing_tickers) - len(relevant)

        for idx, item in enumerate(relevant, 1):
            if progress_callback:
                progress_callback(idx, len(relevant), item["ticker"])

            exchange = item.get("exchange") or derive_exchange_from_ticker(item["ticker"]) or ""
            currency = item.get("currencyCode") or EXCHANGE_CURRENCY_MAP.get(exchange.upper())

            stmt = (
                insert(Instrument)
                .values(
                    ticker=item["ticker"],
                    name=item.get("name"),
                    short_name=item.get("shortName"),
                    currency_code=currency,
                    isin=item.get("isin"),
                    instrument_type=item.get("type"),
                    exchange=exchange or None,
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
                        "currency_code": literal_column(
                            "COALESCE(EXCLUDED.currency_code, instruments.currency_code)"
                        ),
                        "isin": item.get("isin"),
                        "instrument_type": item.get("type"),
                        "exchange": literal_column(
                            "COALESCE(EXCLUDED.exchange, instruments.exchange)"
                        ),
                        "min_trade_quantity": item.get("minTradeQuantity"),
                        "max_open_quantity": item.get("maxOpenQuantity"),
                        "updated_at": now,
                    },
                )
            )
            self.session.execute(stmt)

            if idx % 50 == 0:
                self.session.commit()

        # Back-fill NULL currency from exchange map for any instrument still missing it
        backfill = (
            self.session.query(Instrument)
            .filter(
                Instrument.currency_code.is_(None),
                Instrument.exchange.isnot(None),
            )
            .all()
        )
        backfilled = 0
        for inst in backfill:
            derived = EXCHANGE_CURRENCY_MAP.get((inst.exchange or "").upper())
            if derived:
                inst.currency_code = derived
                backfilled += 1

        self.session.commit()

        result = {
            "total_in_db": total,
            "found_in_catalogue": len(relevant),
            "not_in_catalogue": not_in_catalogue,
            "backfilled_currency": backfilled,
        }
        print(
            f"[t212-reload] Complete — {len(relevant)} updated from catalogue, "
            f"{not_in_catalogue} not in catalogue, {backfilled} currency back-filled."
        )
        return result

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

            # Ensure all tickers referenced by this page exist in instruments
            batch_tickers = {
                (item.get("order", {}).get("ticker")
                 or (item.get("order", {}).get("instrument") or {}).get("ticker"))
                for item in items
            } - {None}
            existing = {
                r[0] for r in self.session.query(Instrument.ticker)
                .filter(Instrument.ticker.in_(batch_tickers)).all()
            }
            for missing in batch_tickers - existing:
                self.session.execute(
                    insert(Instrument)
                    .values(ticker=missing, created_at=now, updated_at=now)
                    .on_conflict_do_nothing(index_elements=["ticker"])
                )
            if batch_tickers - existing:
                self.session.flush()

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
        from app.enrichment.ticker_mapper import (
            EXCHANGE_COUNTRY_MAP, EXCHANGE_CURRENCY_MAP, normalize_exchange,
        )
        from app.enrichment.fmp_enricher import FmpDividendEnricher
        from app.enrichment.claude_enricher import ClaudeDescriptionEnricher

        fmp = FmpDividendEnricher(_settings.fmp_api_key) if _settings.fmp_api_key else None
        claude = ClaudeDescriptionEnricher(_settings.anthropic_api_key) if _settings.anthropic_api_key else None

        # Only enrich instruments this user currently holds
        held_tickers = self._held_tickers()
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
            inferred = EXCHANGE_COUNTRY_MAP.get(
                normalize_exchange(inst.exchange) or (inst.exchange or "").upper()
            )
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
            try:
                yf_ticker, info, ticker_obj = self._resolve_yfinance_metadata(instrument, yf)
                if not yf_ticker or not info:
                    continue
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
                        normalize_exchange(instrument.exchange) or (instrument.exchange or "").upper()
                    )

                if info.get("marketCap"):
                    instrument.market_cap = info["marketCap"]
                fundamentals = _extract_yf_fundamentals(ticker_obj, info)
                if fundamentals.get("eps_ttm") is not None:
                    instrument.eps_ttm = fundamentals["eps_ttm"]
                if fundamentals.get("fcf_per_share_3y_avg") is not None:
                    instrument.fcf_per_share_3y_avg = fundamentals["fcf_per_share_3y_avg"]

                # Refine exchange: yfinance returns a human-readable fullExchangeName
                # (e.g. "NasdaqGS", "London") which we normalise back to our
                # internal exchange codes so future ticker derivation still works.
                yf_exchange = _normalised_yf_exchange(info)
                if yf_exchange:
                    current_exchange = normalize_exchange(instrument.exchange)
                    if not current_exchange or current_exchange == "US":
                        instrument.exchange = yf_exchange

                if not instrument.currency_code and instrument.exchange:
                    instrument.currency_code = EXCHANGE_CURRENCY_MAP.get(
                        normalize_exchange(instrument.exchange) or (instrument.exchange or "").upper()
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

    def enrich_instruments_openfigi(self, batch_size: int = 500) -> dict:
        """Enrich instruments with FIGI, MIC code, and security type via OpenFIGI.

        OpenFIGI is fast (batches of 100 ISINs per request) so we can process
        a large batch in one run. Instruments that already have a FIGI and were
        enriched within 30 days are skipped. Instruments without an ISIN fall
        back to ticker-based lookup.

        Returns a summary dict with counts.
        """
        from app.enrichment.openfigi_enricher import OpenFigiEnricher
        from app.config import settings as _settings

        enricher = OpenFigiEnricher(api_key=_settings.openfigi_api_key)
        cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=30)
        now = datetime.datetime.utcnow()

        instruments = (
            self.session.query(Instrument)
            .filter(
                (Instrument.last_figi_enriched_at == None) |  # noqa: E711
                (Instrument.last_figi_enriched_at < cutoff)
            )
            .order_by(Instrument.last_figi_enriched_at.asc().nullsfirst())
            .limit(batch_size)
            .all()
        )

        if not instruments:
            print("[openfigi] All instruments up to date.")
            return {"enriched": 0, "no_match": 0, "total": 0}

        print(f"[openfigi] Looking up {len(instruments)} instruments...")
        results = enricher.enrich(instruments)

        enriched = no_match = 0
        for instrument in instruments:
            data = results.get(instrument.ticker)
            instrument.last_figi_enriched_at = now

            if not data:
                no_match += 1
                continue

            # Write all returned fields — never overwrite with None
            if data.get("figi"):
                instrument.figi = data["figi"]
            if data.get("composite_figi"):
                instrument.composite_figi = data["composite_figi"]
            if data.get("share_class_figi"):
                instrument.share_class_figi = data["share_class_figi"]
            if data.get("mic_code"):
                instrument.mic_code = data["mic_code"]
            if data.get("security_type"):
                instrument.security_type = data["security_type"]
            if data.get("security_type2"):
                instrument.security_type2 = data["security_type2"]
            if data.get("market_sector"):
                instrument.market_sector = data["market_sector"]

            # Use MIC code to refine exchange if still coarse (e.g. "US")
            if data.get("mic_code") and (
                not instrument.exchange or instrument.exchange.upper() in ("US",)
            ):
                from app.enrichment.openfigi_enricher import _MIC_TO_OUR_EXCHANGE
                refined = _MIC_TO_OUR_EXCHANGE.get(data["mic_code"])
                if refined:
                    instrument.exchange = refined

            enriched += 1

        self.session.commit()
        print(f"[openfigi] Done — enriched {enriched}, no match {no_match}, "
              f"total processed {len(instruments)}.")
        return {"enriched": enriched, "no_match": no_match, "total": len(instruments)}

    def enrich_stale_instruments_batch(self, batch_size: int = 300) -> int:
        """Progressively enrich uninitialised or stale instruments.

        Called by the nightly scheduler after user syncs complete. Processes up
        to batch_size instruments per run so yfinance rate limits are respected
        without the job running for hours. Held instruments are always processed
        first to keep portfolio data fresh; the rest of the catalogue follows in
        staleness order.

        Returns the number of instruments enriched.
        """
        import yfinance as yf
        from app.enrichment.ticker_mapper import (
            EXCHANGE_COUNTRY_MAP, EXCHANGE_CURRENCY_MAP, normalize_exchange,
        )
        from app.enrichment.fmp_enricher import FmpDividendEnricher
        from app.config import settings as _settings

        fmp = FmpDividendEnricher(_settings.fmp_api_key) if _settings.fmp_api_key else None
        cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=7)
        now = datetime.datetime.utcnow()

        # Held tickers across all users — these get priority
        held_tickers = {
            r[0] for r in self.session.execute(
                __import__("sqlalchemy").text("SELECT DISTINCT ticker FROM positions")
            )
        }

        # Priority 1: held instruments that are stale / never enriched
        priority = (
            self.session.query(Instrument)
            .filter(Instrument.ticker.in_(held_tickers))
            .filter(
                (Instrument.last_enriched_at == None) |  # noqa: E711
                (Instrument.last_enriched_at < cutoff)
            )
            .order_by(Instrument.last_enriched_at.asc().nullsfirst())
            .limit(batch_size)
            .all()
        )

        # Priority 2: fill remaining slots with non-held stale instruments
        remaining = batch_size - len(priority)
        if remaining > 0:
            priority_tickers = {i.ticker for i in priority}
            others = (
                self.session.query(Instrument)
                .filter(~Instrument.ticker.in_(held_tickers | priority_tickers))
                .filter(
                    (Instrument.last_enriched_at == None) |  # noqa: E711
                    (Instrument.last_enriched_at < cutoff)
                )
                .order_by(Instrument.last_enriched_at.asc().nullsfirst())
                .limit(remaining)
                .all()
            )
        else:
            others = []

        instruments = priority + others
        if not instruments:
            print("[enrich-batch] All instruments up to date.")
            return 0

        print(f"[enrich-batch] Enriching {len(instruments)} stale instruments "
              f"({len(priority)} held-priority, {len(others)} catalogue)...")
        enriched = 0

        for instrument in instruments:
            try:
                yf_ticker, info, ticker_obj = self._resolve_yfinance_metadata(instrument, yf)
            except Exception as exc:
                print(f"  [enrich-batch] {instrument.ticker}: {exc}")
                continue

            if not yf_ticker or not info:
                continue

            try:
                if info.get("longBusinessSummary"):
                    instrument.description = info["longBusinessSummary"]
                if info.get("sector"):
                    instrument.sector = info["sector"]
                if info.get("industry"):
                    instrument.industry = info["industry"]
                if info.get("country"):
                    instrument.country = info["country"]
                elif not instrument.country:
                    instrument.country = EXCHANGE_COUNTRY_MAP.get(
                        normalize_exchange(instrument.exchange) or (instrument.exchange or "").upper()
                    )
                if info.get("marketCap"):
                    instrument.market_cap = info["marketCap"]
                fundamentals = _extract_yf_fundamentals(ticker_obj, info)
                if fundamentals.get("eps_ttm") is not None:
                    instrument.eps_ttm = fundamentals["eps_ttm"]
                if fundamentals.get("fcf_per_share_3y_avg") is not None:
                    instrument.fcf_per_share_3y_avg = fundamentals["fcf_per_share_3y_avg"]
                yf_exchange = _normalised_yf_exchange(info)
                current_exchange = normalize_exchange(instrument.exchange)
                if yf_exchange and (not current_exchange or current_exchange == "US"):
                    instrument.exchange = yf_exchange
                if not instrument.currency_code and instrument.exchange:
                    instrument.currency_code = EXCHANGE_CURRENCY_MAP.get(
                        normalize_exchange(instrument.exchange) or (instrument.exchange or "").upper()
                    )
                # FMP fallback for description
                if not instrument.description and fmp:
                    fmp_desc = fmp.get_description(yf_ticker, instrument.instrument_type or "")
                    if fmp_desc:
                        instrument.description = fmp_desc
                instrument.yf_ticker = yf_ticker
                instrument.last_enriched_at = now
                enriched += 1
            except Exception as exc:
                print(f"  [enrich-batch] {instrument.ticker}: {exc}")

            if enriched % 50 == 0:
                self.session.commit()

        self.session.commit()
        print(f"[enrich-batch] Done — enriched {enriched}/{len(instruments)} instruments.")
        return enriched

    def enrich_all_instruments(self, progress_callback=None) -> dict:
        """Force-enrich every instrument in the database, ignoring the 7-day cache.

        Unlike sync_instrument_metadata this is not scoped to a single user's
        holdings — it processes every row in the instruments table.

        Each field is only written if the external source returns a non-empty
        value, so existing good data is never overwritten by a lookup failure.

        Args:
            progress_callback: optional callable(done, total, ticker) for progress tracking.

        Returns:
            dict with keys: total, enriched, failed, skipped (no yf_ticker derivable)
        """
        import yfinance as yf
        from app.config import settings as _settings
        from app.enrichment.ticker_mapper import (
            EXCHANGE_COUNTRY_MAP, EXCHANGE_CURRENCY_MAP, normalize_exchange
        )
        from app.enrichment.fmp_enricher import FmpDividendEnricher
        from app.enrichment.claude_enricher import ClaudeDescriptionEnricher

        fmp    = FmpDividendEnricher(_settings.fmp_api_key) if _settings.fmp_api_key else None
        claude = ClaudeDescriptionEnricher(_settings.anthropic_api_key) if _settings.anthropic_api_key else None

        instruments = self.session.query(Instrument).order_by(Instrument.ticker).all()
        total    = len(instruments)
        enriched = 0
        failed   = 0
        skipped  = 0
        now      = datetime.datetime.utcnow()

        print(f"[force-enrich] Starting enrichment of all {total} instruments...")

        for idx, instrument in enumerate(instruments, 1):
            # Resolve yfinance ticker — use stored value first, then derive
            try:
                yf_ticker, info, ticker_obj = self._resolve_yfinance_metadata(instrument, yf)
            except Exception as exc:
                failed += 1
                print(f"  [{idx}/{total}] {instrument.ticker}: FAILED - {exc}")
                continue

            if progress_callback:
                progress_callback(idx, total, instrument.ticker)

            if not yf_ticker or not info:
                print(f"  [{idx}/{total}] {instrument.ticker}: no yf_ticker derivable — skipped")
                skipped += 1
                continue

            # Always store the derived yf_ticker so future lookups work
            if not instrument.yf_ticker:
                instrument.yf_ticker = yf_ticker

            # Back-fill currency from exchange map if still NULL
            if not instrument.currency_code and instrument.exchange:
                derived_ccy = EXCHANGE_CURRENCY_MAP.get(
                    normalize_exchange(instrument.exchange) or (instrument.exchange or "").upper()
                )
                if derived_ccy:
                    instrument.currency_code = derived_ccy

            try:

                # Only write if the source returned something useful — never overwrite with empty
                if info.get("longBusinessSummary"):
                    instrument.description = info["longBusinessSummary"]
                if info.get("sector"):
                    instrument.sector = info["sector"]
                if info.get("industry"):
                    instrument.industry = info["industry"]
                if info.get("country"):
                    instrument.country = info["country"]
                if info.get("marketCap"):
                    instrument.market_cap = info["marketCap"]
                fundamentals = _extract_yf_fundamentals(ticker_obj, info)
                if fundamentals.get("eps_ttm") is not None:
                    instrument.eps_ttm = fundamentals["eps_ttm"]
                if fundamentals.get("fcf_per_share_3y_avg") is not None:
                    instrument.fcf_per_share_3y_avg = fundamentals["fcf_per_share_3y_avg"]

                # Country fallback from exchange if yfinance didn't return one
                if not instrument.country and instrument.exchange:
                    inferred = EXCHANGE_COUNTRY_MAP.get(
                        normalize_exchange(instrument.exchange) or (instrument.exchange or "").upper()
                    )
                    if inferred:
                        instrument.country = inferred

                yf_exchange = _normalised_yf_exchange(info)
                current_exchange = normalize_exchange(instrument.exchange)
                if yf_exchange and (not current_exchange or current_exchange == "US"):
                    instrument.exchange = yf_exchange

                if not instrument.currency_code and instrument.exchange:
                    derived_ccy = EXCHANGE_CURRENCY_MAP.get(
                        normalize_exchange(instrument.exchange) or (instrument.exchange or "").upper()
                    )
                    if derived_ccy:
                        instrument.currency_code = derived_ccy

                instrument.last_enriched_at = now

                # Description fallbacks: FMP → Claude → synthesised
                if not instrument.description and fmp:
                    fmp_desc = fmp.get_description(yf_ticker, instrument.instrument_type or "")
                    if fmp_desc:
                        instrument.description = fmp_desc

                if not instrument.description and claude:
                    display_name = instrument.name or instrument.short_name or instrument.ticker
                    claude_desc = claude.get_description(display_name)
                    if claude_desc:
                        instrument.description = claude_desc

                if not instrument.description:
                    instrument.description = _synthesise_description(instrument)

                enriched += 1
                print(f"  [{idx}/{total}] {instrument.ticker} ({yf_ticker}): OK")

            except Exception as exc:
                failed += 1
                print(f"  [{idx}/{total}] {instrument.ticker} ({yf_ticker}): FAILED — {exc}")

            # Commit in batches of 20 so progress is saved incrementally
            if idx % 20 == 0:
                self.session.commit()

        self.session.commit()
        result = {"total": total, "enriched": enriched, "failed": failed, "skipped": skipped}
        print(f"[force-enrich] Complete — {enriched} enriched, {failed} failed, {skipped} skipped.")
        return result


    def refresh_single_instrument(self, ticker: str) -> dict:
        """Force-refresh metadata and fundamentals for a single instrument."""
        import yfinance as yf
        from app.config import settings as _settings
        from app.enrichment.ticker_mapper import (
            EXCHANGE_COUNTRY_MAP, EXCHANGE_CURRENCY_MAP, normalize_exchange,
        )
        from app.enrichment.fmp_enricher import FmpDividendEnricher
        from app.enrichment.claude_enricher import ClaudeDescriptionEnricher

        instrument = self.session.query(Instrument).filter_by(ticker=ticker).first()
        if not instrument:
            raise ValueError(f"Instrument not found: {ticker}")

        fmp = FmpDividendEnricher(_settings.fmp_api_key) if _settings.fmp_api_key else None
        claude = ClaudeDescriptionEnricher(_settings.anthropic_api_key) if _settings.anthropic_api_key else None
        now = datetime.datetime.utcnow()

        yf_ticker, info, ticker_obj = self._resolve_yfinance_metadata(instrument, yf)
        if not yf_ticker or not info:
            raise ValueError(f"No yfinance metadata available for {ticker}")

        if info.get("longBusinessSummary"):
            instrument.description = info["longBusinessSummary"]
        if info.get("sector"):
            instrument.sector = info["sector"]
        if info.get("industry"):
            instrument.industry = info["industry"]
        if info.get("country"):
            instrument.country = info["country"]
        elif not instrument.country:
            instrument.country = EXCHANGE_COUNTRY_MAP.get(
                normalize_exchange(instrument.exchange) or (instrument.exchange or "").upper()
            )
        if info.get("marketCap"):
            instrument.market_cap = info["marketCap"]

        fundamentals = _extract_yf_fundamentals(ticker_obj, info)
        if fundamentals.get("eps_ttm") is not None:
            instrument.eps_ttm = fundamentals["eps_ttm"]
        if fundamentals.get("fcf_per_share_3y_avg") is not None:
            instrument.fcf_per_share_3y_avg = fundamentals["fcf_per_share_3y_avg"]

        yf_exchange = _normalised_yf_exchange(info)
        current_exchange = normalize_exchange(instrument.exchange)
        if yf_exchange and (not current_exchange or current_exchange == "US"):
            instrument.exchange = yf_exchange

        if not instrument.currency_code and instrument.exchange:
            derived_ccy = EXCHANGE_CURRENCY_MAP.get(
                normalize_exchange(instrument.exchange) or (instrument.exchange or "").upper()
            )
            if derived_ccy:
                instrument.currency_code = derived_ccy

        instrument.yf_ticker = yf_ticker
        instrument.last_enriched_at = now

        if not instrument.description and fmp:
            fmp_desc = fmp.get_description(yf_ticker, instrument.instrument_type or "")
            if fmp_desc:
                instrument.description = fmp_desc

        if not instrument.description and claude:
            display_name = instrument.name or instrument.short_name or instrument.ticker
            claude_desc = claude.get_description(display_name)
            if claude_desc:
                instrument.description = claude_desc

        if not instrument.description:
            instrument.description = _synthesise_description(instrument)

        self.session.commit()
        return {
            "ticker": instrument.ticker,
            "yf_ticker": instrument.yf_ticker,
            "eps_ttm": instrument.eps_ttm,
            "fcf_per_share_3y_avg": instrument.fcf_per_share_3y_avg,
        }


def _is_useful_yf_info(info: dict | None) -> bool:
    """Return True when a Yahoo response has enough signal to enrich an instrument."""
    if not isinstance(info, dict):
        return False

    useful_fields = (
        "longBusinessSummary",
        "sector",
        "industry",
        "country",
        "marketCap",
        "longName",
        "shortName",
        "quoteType",
        "fullExchangeName",
        "exchange",
    )
    return any(info.get(field) for field in useful_fields)


def _normalised_yf_exchange(info: dict | None) -> str | None:
    """Map Yahoo exchange labels back to the app's internal exchange codes."""
    from app.enrichment.ticker_mapper import normalize_exchange

    if not isinstance(info, dict):
        return None

    for field in ("exchange", "fullExchangeName"):
        value = info.get(field)
        normalised = normalize_exchange(value)
        if normalised:
            return normalised

    return None


def _extract_yf_fundamentals(ticker_obj, info: dict | None) -> dict[str, float]:
    """Extract EPS TTM and trailing 3-year average FCF/share from Yahoo data."""
    result: dict[str, float] = {}
    if not ticker_obj:
        return result

    if isinstance(info, dict):
        eps = info.get("trailingEps")
        if eps is not None:
            try:
                result["eps_ttm"] = float(eps)
            except (TypeError, ValueError):
                pass

    try:
        cashflow = ticker_obj.cashflow
    except Exception:
        cashflow = None

    if cashflow is None or getattr(cashflow, "empty", True):
        return result

    try:
        if "Free Cash Flow" in cashflow.index:
            fcf_series = cashflow.loc["Free Cash Flow"]
        elif "Operating Cash Flow" in cashflow.index and "Capital Expenditure" in cashflow.index:
            fcf_series = cashflow.loc["Operating Cash Flow"] + cashflow.loc["Capital Expenditure"]
        else:
            return result

        fcf_series = fcf_series.dropna().sort_index(ascending=False)
        if fcf_series.empty:
            return result

        shares = None
        try:
            shares = ticker_obj.fast_info.get("shares")
        except Exception:
            shares = None
        if not shares and isinstance(info, dict):
            shares = info.get("sharesOutstanding")
        if not shares:
            return result

        fcf_ps = fcf_series / float(shares)
        if not fcf_ps.empty:
            result["fcf_per_share_3y_avg"] = float(fcf_ps.head(3).mean())
    except Exception:
        return result

    return result


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
