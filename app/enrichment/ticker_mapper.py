"""
Maps Trading 212 internal tickers to Yahoo Finance ticker symbols.

T212 uses a non-standard format like:
  AAPL_US_EQ   → US equity on NASDAQ/NYSE
  VWRPl_EQ     → UK equity on LSE (lowercase 'l' suffix before _EQ)
  SAP_XETR_EQ  → German equity on XETR

Yahoo Finance expects:
  AAPL         → US
  VWRP.L       → London
  SAP.DE       → Frankfurt
"""
import re

# Known T212 exchange identifiers → Yahoo Finance suffix
EXCHANGE_COUNTRY_MAP: dict[str, str] = {
    # United Kingdom
    "LSE": "United Kingdom",
    "XLON": "United Kingdom",
    # United States
    "US": "United States",
    "NASDAQ": "United States",
    "NYSE": "United States",
    "BATS": "United States",
    "ARCA": "United States",
    "AMEX": "United States",
    # Germany
    "XETR": "Germany",
    "FRA": "Germany",
    # France
    "XPAR": "France",
    # Netherlands
    "XAMS": "Netherlands",
    # Spain
    "XMAD": "Spain",
    # Italy
    "XMIL": "Italy",
    # Switzerland
    "XSWX": "Switzerland",
    # Sweden
    "XSTO": "Sweden",
    # Denmark
    "XCSE": "Denmark",
    # Norway
    "XOSL": "Norway",
    # Canada
    "TSX": "Canada",
    "TSXV": "Canada",
    # Australia
    "ASX": "Australia",
    # Hong Kong
    "HKEX": "Hong Kong",
    "XHKG": "Hong Kong",
    # Japan
    "TSE": "Japan",
    # India
    "NSE": "India",
    "BSE": "India",
    # Brazil
    "BVMF": "Brazil",
    # Singapore
    "SGX": "Singapore",
}

EXCHANGE_CURRENCY_MAP: dict[str, str] = {
    # United Kingdom — LSE stocks trade in pence (GBX), not pounds
    "LSE":    "GBX",
    "XLON":   "GBX",
    # United States
    "US":     "USD",
    "NASDAQ": "USD",
    "NYSE":   "USD",
    "BATS":   "USD",
    "ARCA":   "USD",
    "AMEX":   "USD",
    # Germany
    "XETR":   "EUR",
    "FRA":    "EUR",
    # France
    "XPAR":   "EUR",
    # Netherlands
    "XAMS":   "EUR",
    # Spain
    "XMAD":   "EUR",
    # Italy
    "XMIL":   "EUR",
    # Switzerland
    "XSWX":   "CHF",
    # Sweden
    "XSTO":   "SEK",
    # Denmark
    "XCSE":   "DKK",
    # Norway
    "XOSL":   "NOK",
    # Canada
    "TSX":    "CAD",
    "TSXV":   "CAD",
    # Australia
    "ASX":    "AUD",
    # Hong Kong
    "HKEX":   "HKD",
    "XHKG":   "HKD",
    # Japan
    "TSE":    "JPY",
    # India
    "NSE":    "INR",
    "BSE":    "INR",
    # Brazil
    "BVMF":   "BRL",
    # Singapore
    "SGX":    "SGD",
}

EXCHANGE_SUFFIX_MAP: dict[str, str] = {
    # United Kingdom
    "LSE": ".L",
    "XLON": ".L",
    # United States
    "US": "",
    "NASDAQ": "",
    "NYSE": "",
    "BATS": "",
    "ARCA": "",
    "AMEX": "",
    # Germany
    "XETR": ".DE",
    "FRA": ".F",
    # France
    "XPAR": ".PA",
    # Netherlands
    "XAMS": ".AS",
    # Spain
    "XMAD": ".MC",
    # Italy
    "XMIL": ".MI",
    # Switzerland
    "XSWX": ".SW",
    # Sweden
    "XSTO": ".ST",
    # Denmark
    "XCSE": ".CO",
    # Norway
    "XOSL": ".OL",
    # Canada
    "TSX": ".TO",
    "TSXV": ".V",
    # Australia
    "ASX": ".AX",
    # Hong Kong
    "HKEX": ".HK",
    "XHKG": ".HK",
    # Japan
    "TSE": ".T",
    # India
    "NSE": ".NS",
    "BSE": ".BO",
    # Brazil
    "BVMF": ".SA",
    # Singapore
    "SGX": ".SI",
}

YF_EXCHANGE_ALIAS_MAP: dict[str, str] = {
    "LONDON": "LSE",
    "LSE": "LSE",
    "XLON": "XLON",
    "NASDAQ": "NASDAQ",
    "NASDAQ GS": "NASDAQ",
    "NASDAQ GM": "NASDAQ",
    "NASDAQ CM": "NASDAQ",
    "NASDAQGS": "NASDAQ",
    "NASDAQGM": "NASDAQ",
    "NASDAQCM": "NASDAQ",
    "NYSE": "NYSE",
    "NYSEARCA": "ARCA",
    "ARCA": "ARCA",
    "BATS": "BATS",
    "NYSEAMERICAN": "AMEX",
    "NYSE MKT": "AMEX",
    "AMEX": "AMEX",
    "XETRA": "XETR",
    "XETR": "XETR",
    "FRANKFURT": "FRA",
    "FRA": "FRA",
    "PARIS": "XPAR",
    "XPAR": "XPAR",
    "AMSTERDAM": "XAMS",
    "XAMS": "XAMS",
    "MADRID": "XMAD",
    "XMAD": "XMAD",
    "MILAN": "XMIL",
    "XMIL": "XMIL",
    "SIX": "XSWX",
    "XSWX": "XSWX",
    "STOCKHOLM": "XSTO",
    "XSTO": "XSTO",
    "COPENHAGEN": "XCSE",
    "XCSE": "XCSE",
    "OSLO": "XOSL",
    "XOSL": "XOSL",
    "TORONTO": "TSX",
    "TSX": "TSX",
    "TSXV": "TSXV",
    "ASX": "ASX",
    "HKEX": "HKEX",
    "XHKG": "XHKG",
    "TSE": "TSE",
    "NSE": "NSE",
    "BSE": "BSE",
    "BVMF": "BVMF",
    "SGX": "SGX",
}


def derive_exchange_from_ticker(t212_ticker: str) -> str | None:
    """
    Parse the exchange code directly from a T212 ticker string.

    T212 does not include exchange in its API response, but encodes it in the
    ticker format itself:

      AAPL_US_EQ    → "US"   (US-listed; yfinance enrichment refines to NYSE/Nasdaq)
      SAP_XETR_EQ   → "XETR"
      NESN_XSWX_EQ  → "XSWX"
      VWRPl_EQ      → "LSE"  (trailing lowercase 'l' before _EQ = London)
      AIRl_EQ       → "LSE"
    """
    if not t212_ticker:
        return None

    # Trailing lowercase 'l' before _EQ = London Stock Exchange
    # e.g. VWRPl_EQ, ARMGl_EQ — distinguish from tickers that happen to end in 'l'
    if t212_ticker.endswith("l_EQ") and "_US_" not in t212_ticker:
        return "LSE"

    # Standard pattern: SYMBOL_EXCHANGE_TYPE
    # The exchange code is always the second-to-last underscore-separated segment.
    # e.g. AAPL_US_EQ → ["AAPL", "US", "EQ"] → candidate = "US"
    #      SAP_XETR_EQ → ["SAP", "XETR", "EQ"] → candidate = "XETR"
    #      BRK_B_US_EQ → ["BRK", "B", "US", "EQ"] → candidate = "US"
    parts = t212_ticker.split("_")
    if len(parts) >= 3:
        candidate = parts[-2].upper()
        if candidate in EXCHANGE_SUFFIX_MAP:
            return candidate

    return None


def normalize_exchange(exchange: str | None) -> str | None:
    """Map human-readable or Yahoo exchange labels back to internal exchange codes."""
    if not exchange:
        return None

    key = exchange.strip().upper()
    if not key:
        return None

    if key in EXCHANGE_SUFFIX_MAP:
        return key

    return YF_EXCHANGE_ALIAS_MAP.get(key)


def build_yf_ticker_candidates(
    t212_ticker: str,
    short_name: str | None,
    exchange: str | None,
) -> list[str]:
    """Return best-effort Yahoo Finance ticker candidates in descending confidence order."""
    candidates: list[str] = []
    symbol = _strip_t212_suffix(t212_ticker)
    normalised_exchange = normalize_exchange(exchange)

    def _append(candidate: str | None) -> None:
        if candidate and candidate not in candidates:
            candidates.append(candidate)

    if symbol and normalised_exchange:
        suffix = EXCHANGE_SUFFIX_MAP.get(normalised_exchange)
        if suffix is not None:
            _append(f"{symbol}{suffix}")

    if symbol and t212_ticker.endswith("l_EQ") and not t212_ticker.endswith("_US_EQ"):
        _append(f"{symbol}.L")

    if symbol and "_US_" in t212_ticker:
        _append(symbol)

    _append(symbol)

    fallback = (short_name or "").strip()
    if fallback and _looks_like_symbol(fallback) and fallback.upper() != (symbol or "").upper():
        if normalised_exchange:
            suffix = EXCHANGE_SUFFIX_MAP.get(normalised_exchange)
            if suffix is not None:
                _append(f"{fallback}{suffix}")
        _append(fallback)

    return candidates


def derive_yf_ticker(t212_ticker: str, short_name: str | None, exchange: str | None) -> str | None:
    """
    Best-effort derivation of a Yahoo Finance ticker from T212 data.

    Priority:
      1. Explicit exchange suffix from EXCHANGE_SUFFIX_MAP
      2. T212 ticker pattern heuristics (e.g. trailing lowercase 'l' = LSE)
      3. short_name as-is (works for US equities)

    Returns None if derivation is not possible.
    """
    candidates = build_yf_ticker_candidates(t212_ticker, short_name, exchange)
    return candidates[0] if candidates else None


def _strip_t212_suffix(ticker: str) -> str | None:
    """Remove Trading 212 exchange/type suffixes to get the base symbol."""
    if not ticker:
        return None

    if ticker.endswith("l_EQ") and "_US_" not in ticker:
        return ticker[:-4]

    parts = ticker.split("_")
    if len(parts) >= 3 and parts[-1].upper() == "EQ":
        exchange = parts[-2].upper()
        if exchange in EXCHANGE_SUFFIX_MAP:
            return "_".join(parts[:-2])

    for suffix in ("_US_EQ", "_EQ", "_US"):
        if ticker.upper().endswith(suffix.upper()):
            return ticker[: -len(suffix)]
    return ticker or None


def _looks_like_symbol(value: str) -> bool:
    """Return True for compact symbol-like strings, False for display names."""
    return bool(re.fullmatch(r"[A-Za-z0-9.\-=/]{1,20}", value or ""))
