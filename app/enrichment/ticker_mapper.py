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


def derive_yf_ticker(t212_ticker: str, short_name: str | None, exchange: str | None) -> str | None:
    """
    Best-effort derivation of a Yahoo Finance ticker from T212 data.

    Priority:
      1. Explicit exchange suffix from EXCHANGE_SUFFIX_MAP
      2. T212 ticker pattern heuristics (e.g. trailing lowercase 'l' = LSE)
      3. short_name as-is (works for US equities)

    Returns None if derivation is not possible.
    """
    base = short_name or _strip_t212_suffix(t212_ticker)
    if not base:
        return None

    # Explicit exchange mapping takes precedence
    if exchange:
        suffix = EXCHANGE_SUFFIX_MAP.get(exchange.upper())
        if suffix is not None:
            return f"{base}{suffix}"

    # Heuristic: T212 appends lowercase 'l' before _EQ for LSE stocks
    # e.g. VWRPl_EQ, ARMGl_EQ
    if t212_ticker.endswith("l_EQ") and not t212_ticker.endswith("_US_EQ"):
        return f"{base}.L"

    # US equities typically have no suffix
    if "_US_" in t212_ticker:
        return base

    # Default: return base and let caller handle failures
    return base


def _strip_t212_suffix(ticker: str) -> str | None:
    """Remove common T212 suffixes to get the base symbol."""
    for suffix in ("_US_EQ", "_EQ", "_US"):
        if ticker.upper().endswith(suffix.upper()):
            return ticker[: -len(suffix)]
    return ticker or None
