"""
Yahoo Finance dividend enricher.

Provides:
  - 5 years of historical dividend payments (ex-dates + amounts)
  - Next confirmed ex-dividend date and annual rate
  - Projected future payments based on inferred payment frequency
"""
import datetime
import time
import logging
from typing import Optional
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class HistoricalDividend:
    ex_date: datetime.date
    amount: float
    pay_date: Optional[datetime.date] = None
    currency: Optional[str] = None


@dataclass
class DividendOutlook:
    annual_rate: Optional[float]
    dividend_yield: Optional[float]
    frequency: str  # MONTHLY | QUARTERLY | SEMI_ANNUAL | ANNUAL | IRREGULAR
    next_ex_date: Optional[datetime.date]
    next_pay_date: Optional[datetime.date]
    next_amount: Optional[float]   # last known payment amount as proxy
    projected: list[dict] = field(default_factory=list)  # [{ex_date, amount, is_estimated}]
    # Instrument metadata — populated from yf.info in get_outlook()
    description: Optional[str] = None
    sector: Optional[str] = None
    industry: Optional[str] = None
    country: Optional[str] = None


class YFinanceDividendEnricher:
    def __init__(self, lookback_years: int = 5, request_delay: float = 1.5):
        self.lookback_years = lookback_years
        self.request_delay = request_delay  # seconds between yfinance calls

    def get_history(self, yf_ticker: str) -> list[HistoricalDividend]:
        """Return up to `lookback_years` of historical dividends."""
        try:
            import yfinance as yf  # lazy import

            time.sleep(self.request_delay)
            ticker = yf.Ticker(yf_ticker)
            divs = ticker.dividends

            if divs is None or divs.empty:
                return []

            cutoff = datetime.date.today() - datetime.timedelta(days=self.lookback_years * 365)
            results = []
            for ts, amount in divs.items():
                ex_date = ts.date() if hasattr(ts, "date") else ts
                if ex_date >= cutoff:
                    results.append(HistoricalDividend(ex_date=ex_date, amount=float(amount)))

            logger.info("  [yfinance] %s: %d historical dividends fetched", yf_ticker, len(results))
            return results

        except Exception as exc:
            logger.warning("  [yfinance] %s history failed: %s", yf_ticker, exc)
            return []

    def get_outlook(self, yf_ticker: str, history: list[HistoricalDividend]) -> DividendOutlook:
        """Return upcoming dividend info and project future payments."""
        annual_rate = None
        div_yield = None
        next_ex_date = None

        try:
            import yfinance as yf

            time.sleep(self.request_delay)
            info = yf.Ticker(yf_ticker).info

            annual_rate = info.get("dividendRate") or info.get("trailingAnnualDividendRate")
            div_yield = info.get("dividendYield") or info.get("trailingAnnualDividendYield")

            raw_ex = info.get("exDividendDate")
            if raw_ex:
                next_ex_date = _ts_to_date(raw_ex)

            description = info.get("longBusinessSummary") or None
            sector      = info.get("sector") or None
            industry    = info.get("industry") or None
            country     = info.get("country") or None

        except Exception as exc:
            logger.warning("  [yfinance] %s outlook failed: %s", yf_ticker, exc)
            description = sector = industry = country = None

        frequency = _infer_frequency(history)
        last_amount = history[-1].amount if history else None
        if last_amount is None and annual_rate:
            last_amount = _annual_to_per_payment(annual_rate, frequency)

        projected = _project_payments(
            confirmed_next=next_ex_date,
            last_amount=last_amount,
            frequency=frequency,
            periods=8,  # project up to 8 future payments
        )

        return DividendOutlook(
            annual_rate=annual_rate,
            dividend_yield=div_yield,
            frequency=frequency,
            next_ex_date=next_ex_date,
            next_pay_date=None,  # yfinance doesn't provide this; FMP enricher fills it in
            next_amount=last_amount,
            projected=projected,
            description=description,
            sector=sector,
            industry=industry,
            country=country,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _infer_frequency(history: list[HistoricalDividend]) -> str:
    """Infer payment frequency from the gaps between historical ex-dates."""
    if len(history) < 2:
        return "ANNUAL"

    dates = sorted(d.ex_date for d in history)
    gaps = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]
    avg_gap = sum(gaps) / len(gaps)

    if avg_gap < 45:
        return "MONTHLY"
    elif avg_gap < 120:
        return "QUARTERLY"
    elif avg_gap < 270:
        return "SEMI_ANNUAL"
    else:
        return "ANNUAL"


def _annual_to_per_payment(annual_rate: float, frequency: str) -> float:
    divisors = {"MONTHLY": 12, "QUARTERLY": 4, "SEMI_ANNUAL": 2, "ANNUAL": 1}
    return annual_rate / divisors.get(frequency, 1)


def _project_payments(
    confirmed_next: Optional[datetime.date],
    last_amount: Optional[float],
    frequency: str,
    periods: int,
) -> list[dict]:
    """Project future ex-dates forward from the confirmed or estimated next payment."""
    if last_amount is None:
        return []

    gap_days = {"MONTHLY": 30, "QUARTERLY": 91, "SEMI_ANNUAL": 182, "ANNUAL": 365}.get(
        frequency, 365
    )

    if frequency == "IRREGULAR":
        return []

    today = datetime.date.today()
    start = confirmed_next or (today + datetime.timedelta(days=gap_days))
    is_first_confirmed = confirmed_next is not None

    results = []
    current = start
    for i in range(periods):
        is_estimated = not (i == 0 and is_first_confirmed)
        results.append(
            {
                "ex_date": current,
                "amount": last_amount,
                "is_estimated": is_estimated,
            }
        )
        current = current + datetime.timedelta(days=gap_days)

    return results


def _ts_to_date(value) -> Optional[datetime.date]:
    """Convert Unix timestamp or ISO string to a date."""
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.datetime.utcfromtimestamp(value).date()
        if isinstance(value, str):
            return datetime.date.fromisoformat(value[:10])
        if hasattr(value, "date"):
            return value.date()
    except Exception:
        pass
    return None
