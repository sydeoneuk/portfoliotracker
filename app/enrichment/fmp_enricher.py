"""
Financial Modeling Prep (FMP) dividend enricher.

Provides pay dates and record dates that yfinance lacks, plus a forward
dividend calendar window.

Free tier: 250 requests/day.
Sign up: https://financialmodelingprep.com/developer/docs/

Set FMP_API_KEY in .env to enable. If not set, this enricher is silently skipped.
"""
import datetime
import logging
from typing import Optional
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

FMP_BASE = "https://financialmodelingprep.com/api/v3"


@dataclass
class FmpDividend:
    ex_date: datetime.date
    pay_date: Optional[datetime.date]
    record_date: Optional[datetime.date]
    declaration_date: Optional[datetime.date]
    amount: float
    adj_amount: Optional[float]


class FmpDividendEnricher:
    def __init__(self, api_key: str, lookback_years: int = 5):
        self.api_key = api_key
        self.lookback_years = lookback_years
        self._session = requests.Session()

    def get_history(self, symbol: str) -> list[FmpDividend]:
        """Fetch historical dividends for a symbol (e.g. 'AAPL', 'VWRP.L')."""
        url = f"{FMP_BASE}/historical-price-full/stock_dividend/{symbol}"
        try:
            resp = self._session.get(url, params={"apikey": self.api_key}, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("  [fmp] %s history failed: %s", symbol, exc)
            return []

        historical = data.get("historical", [])
        cutoff = datetime.date.today() - datetime.timedelta(days=self.lookback_years * 365)
        results = []

        for item in historical:
            ex_date = _parse_date(item.get("date"))
            if ex_date and ex_date >= cutoff:
                results.append(
                    FmpDividend(
                        ex_date=ex_date,
                        pay_date=_parse_date(item.get("paymentDate")),
                        record_date=_parse_date(item.get("recordDate")),
                        declaration_date=_parse_date(item.get("declarationDate")),
                        amount=float(item.get("dividend", 0)),
                        adj_amount=_try_float(item.get("adjDividend")),
                    )
                )

        logger.info("  [fmp] %s: %d historical dividends fetched", symbol, len(results))
        return results

    def get_calendar(
        self,
        from_date: Optional[datetime.date] = None,
        to_date: Optional[datetime.date] = None,
    ) -> list[FmpDividend]:
        """
        Fetch the upcoming dividend calendar.
        Defaults to today → +90 days.
        """
        today = datetime.date.today()
        from_date = from_date or today
        to_date = to_date or (today + datetime.timedelta(days=90))

        url = f"{FMP_BASE}/stock_dividend_calendar"
        try:
            resp = self._session.get(
                url,
                params={
                    "from": from_date.isoformat(),
                    "to": to_date.isoformat(),
                    "apikey": self.api_key,
                },
                timeout=10,
            )
            resp.raise_for_status()
            items = resp.json()
        except Exception as exc:
            logger.warning("  [fmp] calendar failed: %s", exc)
            return []

        results = []
        for item in items:
            ex_date = _parse_date(item.get("date"))
            if ex_date:
                results.append(
                    FmpDividend(
                        ex_date=ex_date,
                        pay_date=_parse_date(item.get("paymentDate")),
                        record_date=_parse_date(item.get("recordDate")),
                        declaration_date=_parse_date(item.get("declarationDate")),
                        amount=float(item.get("dividend", 0) or 0),
                        adj_amount=_try_float(item.get("adjDividend")),
                    )
                )

        logger.info("  [fmp] calendar: %d upcoming dividends fetched", len(results))
        return results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_date(value: Optional[str]) -> Optional[datetime.date]:
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _try_float(value) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
