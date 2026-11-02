"""
yfinance price provider.

Fetches end-of-day prices for a batch of instruments and converts each to GBP
using yfinance FX pairs (e.g. USDGBP=X).  Instruments without a yf_ticker are
skipped; the caller should fall back to another source for them.
"""
import logging
import warnings
import math
from typing import Sequence

import yfinance as yf

# yfinance uses pandas sort_index(inplace=True) which is deprecated in pandas 4.x.
# Suppress until yfinance is updated.
warnings.filterwarnings("ignore", message=".*inplace keyword.*sort_index.*", module="yfinance")

from app.prices.base import PriceProvider, PriceResult
from app.valuation import currency_code

log = logging.getLogger(__name__)

_SOURCE = "yfinance"

# Currencies we can convert to GBP via yfinance FX pairs.
# GBX (pence) needs special handling: divide by 100.
_GBX_CURRENCIES = {"GBX", "GBp"}


def _fx_pair(currency: str) -> str | None:
    if currency in ("GBP", "GBX", "GBp"):
        return None        # no conversion needed
    return f"{currency}GBP=X"


def _fetch_fx_rates(currencies: set[str]) -> dict[str, float]:
    """Return {currency: rate_to_gbp} for each non-GBP currency."""
    rates: dict[str, float] = {"GBP": 1.0}
    pairs = {_fx_pair(c) for c in currencies if _fx_pair(c)}
    if not pairs:
        return rates
    try:
        data = yf.download(list(pairs), period="2d", auto_adjust=True, progress=False)
        close = data["Close"] if "Close" in data else data
        for pair in pairs:
            try:
                col = close[pair] if pair in close.columns else close
                rate = float(col.dropna().iloc[-1])
                # pair is e.g. "USDGBP=X" → currency is "USD"
                currency = pair.replace("GBP=X", "")
                if math.isfinite(rate) and rate > 0:
                    rates[currency] = rate
            except Exception:
                pass
    except Exception as exc:
        log.warning("yfinance FX fetch failed: %s", exc)
    return rates


class YFinanceProvider(PriceProvider):
    source = _SOURCE

    def fetch(self, instruments: Sequence[dict]) -> list[PriceResult]:
        """
        instruments: list of {ticker, yf_ticker, currency, exchange}
        """
        # Build yf_ticker → instrument mapping; skip anything without a yf_ticker
        yf_map: dict[str, dict[str, dict]] = {}
        for inst in instruments:
            yf_t = inst.get("yf_ticker") or _derive_yf_ticker(inst)
            if yf_t:
                yf_map.setdefault(yf_t, {})[inst['ticker']] = inst

        if not yf_map:
            return []

        # Fetch prices
        yf_tickers = list(yf_map)
        try:
            raw = yf.download(
                yf_tickers,
                period="2d",
                auto_adjust=True,
                progress=False,
                group_by="ticker",
            )
        except Exception as exc:
            log.error("yfinance download failed: %s", exc)
            return []

        # Extract the most recent close per ticker
        prices_native: dict[str, float] = {}
        for yf_t in yf_tickers:
            try:
                if len(yf_tickers) == 1:
                    close_series = raw["Close"]
                else:
                    close_series = raw[yf_t]["Close"]
                prices_native[yf_t] = float(close_series.dropna().iloc[-1])
            except Exception:
                log.debug("No close price for %s", yf_t)

        # Downloaded closes use Yahoo's quote units, which can differ from an
        # uploaded listing's units (notably GBP versus GBp). Never label those
        # prices using the holding file's currency without checking the source.
        quote_currencies = {}
        for yf_t in prices_native:
            try:
                ccy = currency_code(yf.Ticker(yf_t).fast_info["currency"])
                if ccy:
                    quote_currencies[yf_t] = ccy
            except Exception:
                log.warning("Quote currency unavailable for %s; price skipped", yf_t)

        # Collect currencies needed for FX conversion
        needed_currencies: set[str] = set()
        for yf_t, price in prices_native.items():
            ccy = quote_currencies.get(yf_t)
            if not ccy:
                continue
            if ccy not in ("GBP",) and ccy not in _GBX_CURRENCIES:
                needed_currencies.add(ccy)

        fx = _fetch_fx_rates(needed_currencies)
        fx["GBP"] = 1.0

        results: list[PriceResult] = []
        for yf_t, native_price in prices_native.items():
            ccy = quote_currencies.get(yf_t)
            if not ccy:
                continue

            rate = fx.get(ccy)
            gbp_price = native_price / 100.0 if ccy in _GBX_CURRENCIES else native_price * rate if rate is not None else None
            for inst in yf_map[yf_t].values():
                results.append(PriceResult(
                    ticker=inst["ticker"],
                    price=native_price,
                    price_gbp=gbp_price,
                    currency=ccy,
                ))

        log.info("yfinance: priced %d / %d instruments", len(results), len(instruments))
        return results


def _derive_yf_ticker(inst: dict) -> str | None:
    """
    Heuristic yfinance ticker derivation when yf_ticker is not set.
    Uses exchange/MIC code to append the right suffix.
    """
    ticker = inst.get("ticker", "")
    exchange = (inst.get("exchange") or "").upper()
    mic = (inst.get("mic_code") or "").upper()

    # Strip T212 suffixes like _EQ, _US_EQ
    base = ticker.split("_")[0]

    # London Stock Exchange
    if mic in ("XLON",) or exchange in ("LSE", "LONDON"):
        return f"{base}.L"
    # US exchanges — no suffix needed
    if mic in ("XNAS", "XNYS", "ARCX") or exchange in ("NASDAQ", "NYSE", "NYSE ARCA"):
        return base
    # Euronext
    if mic in ("XAMS",):
        return f"{base}.AS"
    if mic in ("XPAR",):
        return f"{base}.PA"
    if mic in ("XFRA",):
        return f"{base}.F"

    # No known mapping — skip
    return None
