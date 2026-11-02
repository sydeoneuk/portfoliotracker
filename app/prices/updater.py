"""
Price updater: orchestrates price providers and persists results to instrument_prices.

Priority order: t212_api > yfinance > manual
The updater only replaces t212_api rows when called from the API sync; it calls
yfinance for everything that lacks a fresh t212_api price.
"""
import datetime
import logging

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.models.instrument_price import InstrumentPrice
from app.prices.base import PriceResult
from app.prices.yfinance_provider import YFinanceProvider

log = logging.getLogger(__name__)

_STALE_HOURS = 24   # refresh yfinance prices older than this


def upsert_prices(db: Session, results: list[PriceResult], source: str) -> int:
    """Write a list of PriceResults to instrument_prices. Returns rows written."""
    if not results:
        return 0
    now = datetime.datetime.utcnow()
    rows = [
        {
            "ticker":     r.ticker,
            "source":     source,
            "price":      r.price,
            "price_gbp":  r.price_gbp,
            "currency":   r.currency,
            "updated_at": now,
        }
        for r in results
    ]
    stmt = (
        insert(InstrumentPrice)
        .values(rows)
        .on_conflict_do_update(
            index_elements=["ticker", "source"],
            set_={
                "price":      insert(InstrumentPrice).excluded.price,
                "price_gbp":  insert(InstrumentPrice).excluded.price_gbp,
                "currency":   insert(InstrumentPrice).excluded.currency,
                "updated_at": insert(InstrumentPrice).excluded.updated_at,
            },
        )
    )
    db.execute(stmt)
    db.commit()
    return len(rows)


def refresh_yfinance_prices(db: Session, user_id: int | None = None) -> dict:
    from app.account_pricing import refresh_due_prices
    account_quotes = refresh_due_prices(db, user_id)
    from app.reconciliation import refresh_due_mappings
    mapped = refresh_due_mappings(db, user_id)
    result = _refresh_yfinance_prices(db, user_id)
    result["mapped_fetched"] = mapped["fetched"]
    result['account_fetched'] = account_quotes['fetched']
    result['errors'].extend(account_quotes['errors'])
    result["errors"].extend(mapped["errors"])
    return result


def _refresh_yfinance_prices(db: Session, user_id: int | None = None) -> dict:
    """
    Fetch prices from yfinance for instruments that are either:
      - held in any position (optionally filtered by user_id), or
      - lack a price entirely.
    Skips instruments with a fresh t212_api price (< _STALE_HOURS old).
    Returns a summary dict.
    """
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(hours=_STALE_HOURS)

    # Load instruments that need pricing
    uid_filter = "AND p.user_id = :user_id" if user_id else ""
    rows = db.execute(text(f"""
        SELECT DISTINCT
            i.ticker,
            i.yf_ticker,
            COALESCE(i.currency_code, 'GBP') AS currency,
            i.exchange,
            i.mic_code,
            (
                SELECT ip2.updated_at
                FROM instrument_prices ip2
                WHERE ip2.ticker = i.ticker AND ip2.source = 'yfinance'
            ) AS yf_updated_at
        FROM instruments i
        JOIN positions p ON p.ticker = i.ticker
        WHERE 1=1 {uid_filter}
          AND NOT EXISTS (
              SELECT 1 FROM instrument_prices ip
              WHERE ip.ticker = i.ticker
                AND ip.source = 't212_api'
                AND ip.updated_at > :cutoff
          )
    """), {"user_id": user_id, "cutoff": cutoff} if user_id else {"cutoff": cutoff}).fetchall()

    if not rows:
        return {"fetched": 0, "skipped": 0, "errors": []}

    # Skip instruments whose yfinance price is also fresh
    to_fetch = [
        {
            "ticker":   r.ticker,
            "yf_ticker": r.yf_ticker,
            "currency": r.currency,
            "exchange": r.exchange,
            "mic_code": r.mic_code,
        }
        for r in rows
        if r.yf_updated_at is None or r.yf_updated_at < cutoff
    ]
    skipped = len(rows) - len(to_fetch)

    if not to_fetch:
        return {"fetched": 0, "skipped": skipped, "errors": []}

    provider = YFinanceProvider()
    try:
        results = provider.fetch(to_fetch)
    except Exception as exc:
        log.error("yfinance provider failed: %s", exc)
        return {"fetched": 0, "skipped": skipped, "errors": [str(exc)]}

    written = upsert_prices(db, results, source="yfinance")
    return {"fetched": written, "skipped": skipped, "errors": []}
