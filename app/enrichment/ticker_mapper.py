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

EXCHANGE_SUFFIX_MAP: dict[str, str] = {
    # United Kingdom
    "LSE": ".L",
    "XLON": ".L",
    # United States
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
