"""
DividendSync — orchestrates dividend enrichment for all instruments.

Run flow:
  1. Load all instruments that have been synced from T212
  2. Derive/use yf_ticker for each instrument
  3. Fetch historical dividends via yfinance (5 years)
  4. If FMP key available: backfill pay/record dates and fetch upcoming calendar
  5. Compute dividend outlook (frequency, projections)
  6. Upsert everything into dividend_history + dividend_forecast
"""
import datetime
import logging
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy.dialects.postgresql import insert

from app.models import Instrument, DividendHistory, DividendForecast
from app.enrichment.ticker_mapper import build_yf_ticker_candidates, derive_yf_ticker
from app.enrichment.yfinance_enricher import YFinanceDividendEnricher
from app.enrichment.fmp_enricher import FmpDividendEnricher

logger = logging.getLogger(__name__)


class DividendSync:
    def __init__(
        self,
        session: Session,
        fmp_api_key: Optional[str] = None,
        lookback_years: int = 5,
        yf_delay: float = 1.5,
        force_refresh: bool = False,
    ):
        self.session = session
        self.lookback_years = lookback_years
        self.force_refresh = force_refresh
        self.yf_enricher = YFinanceDividendEnricher(
            lookback_years=lookback_years, request_delay=yf_delay
        )
        self.fmp_enricher = FmpDividendEnricher(fmp_api_key, lookback_years) if fmp_api_key else None

    def sync_all(self, tickers: Optional[list[str]] = None):
        """
        Sync dividends for all (or a subset of) instruments.

        Args:
            tickers: Optional list of T212 tickers to restrict the sync.
                     Defaults to instruments the user actually holds (positions
                     + pie holdings), not the full 16k+ metadata catalogue.
        """
        from app.models import Position, PieHolding

        if tickers:
            query = self.session.query(Instrument).filter(Instrument.ticker.in_(tickers))
        else:
            # Only enrich instruments the user currently holds
            held_tickers = {
                r[0]
                for r in self.session.query(Position.ticker).all()
            } | {
                r[0]
                for r in self.session.query(PieHolding.ticker).all()
            }
            query = self.session.query(Instrument).filter(Instrument.ticker.in_(held_tickers))

        instruments = query.all()

        logger.info("Starting dividend sync for %d instruments...", len(instruments))

        # Pre-load FMP calendar once for the next 90 days (saves ~N requests)
        fmp_calendar: dict[str, list] = {}
        if self.fmp_enricher:
            fmp_calendar = self._load_fmp_calendar()

        synced = skipped = failed = 0
        for instrument in instruments:
            yf_ticker = self._resolve_yf_ticker(instrument)
            if not yf_ticker:
                logger.warning("  Skipping %s — could not derive yf_ticker", instrument.ticker)
                skipped += 1
                continue

            try:
                self._sync_instrument(instrument, yf_ticker, fmp_calendar)
                synced += 1
            except Exception as exc:
                logger.error("  Error syncing %s: %s", instrument.ticker, exc)
                failed += 1

        logger.info(
            "Dividend sync complete. synced=%d  skipped=%d  failed=%d",
            synced, skipped, failed,
        )

    def _sync_instrument(
        self,
        instrument: Instrument,
        yf_ticker: str,
        fmp_calendar: dict[str, list],
    ):
        logger.info("Syncing dividends for %s (%s)...", instrument.ticker, yf_ticker)
        now = datetime.datetime.utcnow()

        # ── 1. Historical dividends from yfinance ─────────────────────────
        yf_history = self.yf_enricher.get_history(yf_ticker)

        # ── 2. Backfill pay/record dates from FMP history (optional) ──────
        fmp_history_map: dict[datetime.date, "FmpDividend"] = {}  # type: ignore[name-defined]
        if self.fmp_enricher:
            fmp_history = self.fmp_enricher.get_history(yf_ticker)
            fmp_history_map = {d.ex_date: d for d in fmp_history}

        # ── 3. Upsert dividend_history ────────────────────────────────────
        for div in yf_history:
            fmp = fmp_history_map.get(div.ex_date)
            stmt = (
                insert(DividendHistory)
                .values(
                    ticker=instrument.ticker,
                    ex_date=div.ex_date,
                    pay_date=fmp.pay_date if fmp else None,
                    record_date=fmp.record_date if fmp else None,
                    declaration_date=fmp.declaration_date if fmp else None,
                    amount=div.amount,
                    adj_amount=fmp.adj_amount if fmp else None,
                    currency=instrument.currency_code,
                    source="fmp" if fmp else "yfinance",
                    fetched_at=now,
                )
                .on_conflict_do_update(
                    constraint="uq_dividend_history_ticker_exdate",
                    set_={
                        "amount": div.amount,
                        "pay_date": fmp.pay_date if fmp else DividendHistory.pay_date,
                        "record_date": fmp.record_date if fmp else DividendHistory.record_date,
                        "adj_amount": fmp.adj_amount if fmp else DividendHistory.adj_amount,
                        "source": "fmp" if fmp else "yfinance",
                        "fetched_at": now,
                    },
                )
            )
            self.session.execute(stmt)

        # ── 4. Dividend outlook (frequency + projections) ─────────────────
        outlook = self.yf_enricher.get_outlook(yf_ticker, yf_history)

        # Merge FMP calendar entries for this ticker (confirmed upcoming)
        fmp_upcoming = fmp_calendar.get(yf_ticker, [])
        confirmed_dates = {d.ex_date for d in fmp_upcoming}

        # Remove stale forecasts
        self.session.query(DividendForecast).filter_by(ticker=instrument.ticker).delete()

        # Insert confirmed FMP calendar entries first
        for fmp_div in fmp_upcoming:
            forecast = DividendForecast(
                ticker=instrument.ticker,
                ex_date=fmp_div.ex_date,
                pay_date=fmp_div.pay_date,
                amount=fmp_div.amount,
                is_estimated=False,
                frequency=outlook.frequency,
                annual_rate=outlook.annual_rate,
                dividend_yield=outlook.dividend_yield,
                source="fmp",
                fetched_at=now,
            )
            self.session.add(forecast)

        # Insert projected payments (skip dates already covered by FMP)
        for proj in outlook.projected:
            if proj["ex_date"] in confirmed_dates:
                continue
            forecast = DividendForecast(
                ticker=instrument.ticker,
                ex_date=proj["ex_date"],
                pay_date=None,
                amount=proj["amount"],
                is_estimated=proj["is_estimated"],
                frequency=outlook.frequency,
                annual_rate=outlook.annual_rate,
                dividend_yield=outlook.dividend_yield,
                source="yfinance",
                fetched_at=now,
            )
            self.session.add(forecast)

        # ── 5. Update instrument metadata and mark as synced ─────────────
        instrument.yf_ticker = yf_ticker
        instrument.last_dividend_synced_at = now
        if outlook.description:
            instrument.description = outlook.description
        if outlook.sector:
            instrument.sector = outlook.sector
        if outlook.industry:
            instrument.industry = outlook.industry
        if outlook.country:
            instrument.country = outlook.country
        instrument.last_enriched_at = now

        self.session.commit()
        logger.info(
            "  %s done — %d history, %d forecast rows",
            instrument.ticker,
            len(yf_history),
            len(fmp_upcoming) + len(outlook.projected),
        )

    def _resolve_yf_ticker(self, instrument: Instrument) -> Optional[str]:
        """Prefer a fresh derived ticker so stale stored mappings can self-correct."""
        derived = derive_yf_ticker(instrument.ticker, instrument.short_name, instrument.exchange)
        if derived:
            return derived
        if instrument.yf_ticker:
            return instrument.yf_ticker

        candidates = build_yf_ticker_candidates(
            instrument.ticker, instrument.short_name, instrument.exchange
        )
        return candidates[0] if candidates else None

    def _load_fmp_calendar(self) -> dict[str, list]:
        """Load FMP dividend calendar grouped by symbol."""
        if not self.fmp_enricher:
            return {}
        items = self.fmp_enricher.get_calendar()
        grouped: dict[str, list] = {}
        for item in items:
            # FMP returns exchange-suffixed symbols, e.g. "VWRP.L"
            # We index by symbol so the per-instrument sync can look up quickly
            symbol = getattr(item, "symbol", None)
            if symbol:
                grouped.setdefault(symbol, []).append(item)
        return grouped
