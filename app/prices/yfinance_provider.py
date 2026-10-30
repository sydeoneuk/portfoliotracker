"""
yfinance price provider.

Fetches end-of-day prices for a batch of instruments and converts each to GBP
using yfinance FX pairs (e.g. USDGBP=X).  Instruments without a yf_ticker are
skipped; the caller should fall back to another source for them.
"""
import logging
import warnings
from typing import Sequence

import yfinance as yf

# yfinance uses pandas sort_index(inplace=True) which is deprecated in pandas 4.x.
# Suppress until yfinance is updated.
warnings.filterwarnings("ignore", message=".*inplace keyword.*sort_index.*", module="yfinance")

from app.prices.base import PriceProvider, PriceResult

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
        yf_map: dict[str, dict] = {}
        for inst in instruments:
            yf_t = inst.get("yf_ticker") or _derive_yf_ticker(inst)
            if yf_t:
                yf_map[yf_t] = inst

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

        # Collect currencies needed for FX conversion
        needed_currencies: set[str] = set()
        for yf_t, price in prices_native.items():
            inst = yf_map[yf_t]
            ccy = inst.get("currency") or "GBP"
            if ccy not in ("GBP",) and ccy not in _GBX_CURRENCIES:
                needed_currencies.add(ccy)

        fx = _fetch_fx_rates(needed_currencies)
        fx["GBP"] = 1.0

        results: list[PriceResult] = []
        for yf_t, native_price in prices_native.items():
            inst = yf_map[yf_t]
            ccy = inst.get("currency") or "GBP"

            if ccy in _GBX_CURRENCIES:
                # Prices in pence — convert to pounds for price, gbp = /100
                gbp_price = native_price / 100.0
                results.append(PriceResult(
                    ticker=inst["ticker"],
                    price=native_price,
                    price_gbp=gbp_price,
                    currency=ccy,
                ))
            else:
                rate = fx.get(ccy, 1.0)
                results.append(PriceResult(
                    ticker=inst["ticker"],
                    price=native_price,
                    price_gbp=native_price * rate,
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
