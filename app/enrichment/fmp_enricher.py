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

FMP_BASE = "https://financialmodelingprep.com/stable"


@dataclass
class FmpDividend:
    symbol: Optional[str]
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
        try:
            items = self._get_json("/dividends", {"symbol": symbol})
        except Exception as exc:
            logger.warning("  [fmp] %s history failed: %s", symbol, exc)
            return []

        cutoff = datetime.date.today() - datetime.timedelta(days=self.lookback_years * 365)
        results = []

        for item in items:
            ex_date = _parse_date(
                item.get("date") or item.get("exDate") or item.get("exDividendDate")
            )
            if ex_date and ex_date >= cutoff:
                results.append(
                    FmpDividend(
                        symbol=item.get("symbol") or symbol,
                        ex_date=ex_date,
                        pay_date=_parse_date(item.get("paymentDate") or item.get("payDate")),
                        record_date=_parse_date(item.get("recordDate")),
                        declaration_date=_parse_date(item.get("declarationDate")),
                        amount=float(item.get("dividend", 0) or 0),
                        adj_amount=_try_float(item.get("adjDividend")),
                    )
                )

        logger.info("  [fmp] %s: %d historical dividends fetched", symbol, len(results))
        return results

    def get_description(self, symbol: str, instrument_type: str = "") -> Optional[str]:
        """Fetch a description for a symbol, trying ETF info then company profile.

        Args:
            symbol: yfinance-style symbol, e.g. 'VWRP.L' or 'AAPL'
            instrument_type: T212 instrument type hint ('ETF', 'STOCK', etc.)
        """
        # Try ETF endpoint first for ETFs/funds, or as fallback for unknowns
        is_etf = (instrument_type or "").upper() in ("ETF", "FUND", "")
        if is_etf:
            desc = self._fetch_etf_description(symbol)
            if desc:
                return desc

        # Try company profile (works for stocks and sometimes ETFs too)
        desc = self._fetch_profile_description(symbol)
        if desc:
            return desc

        # Last resort: ETF endpoint even if type suggests stock
        if not is_etf:
            return self._fetch_etf_description(symbol)

        return None

    def _fetch_etf_description(self, symbol: str) -> Optional[str]:
        try:
            data = self._get_json("/etf/info", {"symbol": symbol})
            items = data if isinstance(data, list) else [data]
            for item in items:
                desc = (item.get("description") or "").strip()
                if desc:
                    return desc
        except Exception as exc:
            logger.debug("  [fmp] ETF description %s failed: %s", symbol, exc)
        return None

    def _fetch_profile_description(self, symbol: str) -> Optional[str]:
        try:
            data = self._get_json("/profile", {"symbol": symbol})
            items = data if isinstance(data, list) else [data]
            for item in items:
                desc = (item.get("description") or "").strip()
                if desc:
                    return desc
        except Exception as exc:
            logger.debug("  [fmp] profile description %s failed: %s", symbol, exc)
        return None

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

        try:
            items = self._get_json(
                "/dividends-calendar",
                {
                    "from": from_date.isoformat(),
                    "to": to_date.isoformat(),
                },
            )
        except Exception as exc:
            logger.warning("  [fmp] calendar failed: %s", exc)
            return []

        results = []
        for item in items:
            ex_date = _parse_date(
                item.get("date") or item.get("exDate") or item.get("exDividendDate")
            )
            if ex_date and from_date <= ex_date <= to_date:
                results.append(
                    FmpDividend(
                        symbol=item.get("symbol"),
                        ex_date=ex_date,
                        pay_date=_parse_date(item.get("paymentDate") or item.get("payDate")),
                        record_date=_parse_date(item.get("recordDate")),
                        declaration_date=_parse_date(item.get("declarationDate")),
                        amount=float(item.get("dividend", 0) or 0),
                        adj_amount=_try_float(item.get("adjDividend")),
                    )
                )

        logger.info("  [fmp] calendar: %d upcoming dividends fetched", len(results))
        return results

    def _get_json(self, path: str, params: dict | None = None):
        """Call an FMP stable endpoint and normalise common response wrappers."""
        url = f"{FMP_BASE}{path}"
        resp = self._session.get(
            url,
            params={**(params or {}), "apikey": self.api_key},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

        if isinstance(data, dict) and data.get("Error Message"):
            raise ValueError(data["Error Message"])

        if isinstance(data, dict):
            for key in ("data", "historical", "dividends", "results"):
                value = data.get(key)
                if isinstance(value, list):
                    return value

        return data


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
