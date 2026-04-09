"""
OpenFIGI enricher.

Maps instruments to their Bloomberg FIGI identifiers and enriches them with:
  - figi            — FIGI for this specific listing
  - composite_figi  — Composite FIGI across all listings
  - share_class_figi
  - mic_code        — ISO MIC exchange code (XLON, XNAS, XNYS, XETR, …)
  - security_type   — e.g. "Common Stock", "ETP", "Mutual Fund"
  - security_type2  — secondary classification
  - market_sector   — e.g. "Equity", "Index", "Commodity"

API docs: https://www.openfigi.com/api
  - No key required for basic use (25 req / 10 s, 100 ISINs per request)
  - Free key raises limit to 25 req / s
"""

import time
import logging
import requests

logger = logging.getLogger(__name__)

# How OpenFIGI exchange codes (exchCode) map to the exchange codes we store.
# Used to pick the best match when an ISIN resolves to multiple listings.
_EXCH_CODE_TO_OUR_EXCHANGE: dict[str, set[str]] = {
    "LN": {"LSE", "XLON"},
    "GY": {"XETR", "FRA"},
    "FP": {"XPAR"},
    "NA": {"XAMS"},
    "SM": {"XMAD"},
    "IM": {"XMIL"},
    "SW": {"XSWX"},
    "SS": {"XSTO"},
    "DC": {"XCSE"},
    "OL": {"XOSL"},
    "CN": {"TSX", "TSXV"},
    "AU": {"ASX"},
    "HK": {"HKEX", "XHKG"},
    "JT": {"TSE"},
    "IS": {"NSE", "BSE"},
    "BZ": {"BVMF"},
    "SP": {"SGX"},
    # US exchanges — exchCode varies (UN=NYSE, UW=NASDAQ NMS, UA=AMEX, etc.)
    "UN": {"NYSE"},
    "UW": {"NASDAQ"},
    "UA": {"AMEX"},
    "UP": {"BATS", "ARCA"},
    "UF": {"BATS"},
}

# Build the reverse map: our exchange code → set of OpenFIGI exchCodes
_OUR_EXCHANGE_TO_EXCH_CODES: dict[str, set[str]] = {}
for exch_code, our_exchanges in _EXCH_CODE_TO_OUR_EXCHANGE.items():
    for our_ex in our_exchanges:
        _OUR_EXCHANGE_TO_EXCH_CODES.setdefault(our_ex, set()).add(exch_code)

# Generic "US" maps to all US exchange codes
_OUR_EXCHANGE_TO_EXCH_CODES["US"] = {"UN", "UW", "UA", "UP", "UF", "UR", "UU"}

# MIC → our exchange (for matching via micCode field)
_MIC_TO_OUR_EXCHANGE: dict[str, str] = {
    "XLON": "LSE", "XNAS": "NASDAQ", "XNYS": "NYSE", "XETR": "XETR",
    "XPAR": "XPAR", "XAMS": "XAMS", "XMAD": "XMAD", "XMIL": "XMIL",
    "XSWX": "XSWX", "XSTO": "XSTO", "XCSE": "XCSE", "XOSL": "XOSL",
    "XTSE": "TSX",  "XASX": "ASX",  "XHKG": "XHKG", "XTKS": "TSE",
    "XNSE": "NSE",  "XBOM": "BSE",  "BVMF": "BVMF", "XSES": "SGX",
}


class OpenFigiEnricher:
    """Batch-enriches instruments via the OpenFIGI mapping API."""

    BASE_URL = "https://api.openfigi.com/v3/mapping"

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key
        # Without key: 10 identifiers/request, 25 req / 10 s → sleep 0.4 s
        # With key:   100 identifiers/request, 25 req / s    → sleep 0.04 s
        self.batch_size = 100 if api_key else 10
        self._delay = 0.04 if api_key else 0.4
        self._session = requests.Session()
        if api_key:
            self._session.headers["X-OPENFIGI-APIKEY"] = api_key
        self._session.headers["Content-Type"] = "application/json"

    # ── Public API ─────────────────────────────────────────────────────────

    def enrich(self, instruments: list) -> dict[str, dict]:
        """
        Look up each instrument by ISIN (preferred) or ticker and return a
        dict mapping ticker → enrichment data dict.

        Instruments without an ISIN and without a recognisable ticker are
        skipped gracefully.
        """
        results: dict[str, dict] = {}

        # Split into ISIN-able and fallback groups
        isin_instruments = [i for i in instruments if i.isin]
        no_isin = [i for i in instruments if not i.isin]

        if isin_instruments:
            results.update(self._lookup_by_isin(isin_instruments))

        if no_isin:
            results.update(self._lookup_by_ticker(no_isin))

        return results

    # ── Internal lookup methods ────────────────────────────────────────────

    def _lookup_by_isin(self, instruments: list) -> dict[str, dict]:
        """Batch-lookup instruments by ISIN."""
        results = {}
        for batch_start in range(0, len(instruments), self.batch_size):
            batch = instruments[batch_start: batch_start + self.batch_size]
            payload = [{"idType": "ID_ISIN", "idValue": i.isin} for i in batch]
            raw = self._post(payload)
            if raw:
                for instrument, response_item in zip(batch, raw):
                    best = self._pick_best_match(response_item, instrument)
                    if best:
                        results[instrument.ticker] = best
        return results

    def _lookup_by_ticker(self, instruments: list) -> dict[str, dict]:
        """Batch-lookup instruments that have no ISIN using ticker + exchange."""
        results = {}
        for batch_start in range(0, len(instruments), self.batch_size):
            batch = instruments[batch_start: batch_start + self.batch_size]
            payload = []
            for inst in batch:
                entry: dict = {"idType": "TICKER", "idValue": inst.short_name or inst.ticker}
                # Narrow by market sector to reduce noise
                entry["marketSecDes"] = "Equity"
                # Optionally narrow by exchange code if we know it
                exch_codes = _OUR_EXCHANGE_TO_EXCH_CODES.get((inst.exchange or "").upper())
                if exch_codes:
                    # OpenFIGI only accepts one exchCode per query — pick first
                    entry["exchCode"] = next(iter(exch_codes))
                payload.append(entry)

            raw = self._post(payload)
            if raw:
                for instrument, response_item in zip(batch, raw):
                    best = self._pick_best_match(response_item, instrument)
                    if best:
                        results[instrument.ticker] = best
        return results

    # ── Match selection ────────────────────────────────────────────────────

    def _pick_best_match(self, response_item: dict, instrument) -> dict | None:
        """
        Choose the most appropriate result from potentially multiple listings.

        Priority:
          1. Match on MIC code if we know it
          2. Match on exchCode if we know our exchange
          3. Prefer "Common Stock" / "ETP" over other security types
          4. Fall back to first result
        """
        candidates = response_item.get("data", [])
        if not candidates:
            if "warning" in response_item:
                logger.debug("OpenFIGI no match for %s: %s", instrument.ticker, response_item["warning"])
            return None

        if len(candidates) == 1:
            return self._normalise(candidates[0])

        our_exchange = (instrument.exchange or "").upper()

        # Try MIC match first
        known_mic = _OUR_EXCHANGE_TO_MIC.get(our_exchange)
        if known_mic:
            mic_matches = [c for c in candidates if c.get("micCode") == known_mic]
            if mic_matches:
                return self._normalise(mic_matches[0])

        # Try exchCode match
        known_exch_codes = _OUR_EXCHANGE_TO_EXCH_CODES.get(our_exchange, set())
        if known_exch_codes:
            exch_matches = [c for c in candidates if c.get("exchCode") in known_exch_codes]
            if exch_matches:
                return self._normalise(exch_matches[0])

        # Prefer equity security types
        preferred_types = {"Common Stock", "ETP", "ETF", "Mutual Fund", "Preference"}
        type_matches = [c for c in candidates if c.get("securityType") in preferred_types]
        if type_matches:
            return self._normalise(type_matches[0])

        return self._normalise(candidates[0])

    @staticmethod
    def _normalise(raw: dict) -> dict:
        """Reshape a raw OpenFIGI result into our storage format."""
        return {
            "figi":             raw.get("figi"),
            "composite_figi":   raw.get("compositeFigi"),
            "share_class_figi": raw.get("shareClassFigi"),
            "mic_code":         raw.get("micCode"),
            "security_type":    raw.get("securityType"),
            "security_type2":   raw.get("securityType2"),
            "market_sector":    raw.get("marketSector"),
            "name":             raw.get("name"),       # useful cross-check
            "exch_code":        raw.get("exchCode"),   # Bloomberg exchange code
        }

    # ── HTTP ──────────────────────────────────────────────────────────────

    def _post(self, payload: list[dict]) -> list[dict] | None:
        """POST a batch to the OpenFIGI mapping endpoint with retry on 429."""
        for attempt in range(3):
            try:
                time.sleep(self._delay)
                resp = self._session.post(self.BASE_URL, json=payload, timeout=15)

                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", 10))
                    logger.warning("OpenFIGI rate limited — sleeping %ds", retry_after)
                    time.sleep(retry_after)
                    continue

                resp.raise_for_status()
                return resp.json()

            except requests.RequestException as exc:
                logger.warning("OpenFIGI request failed (attempt %d): %s", attempt + 1, exc)
                time.sleep(2 ** attempt)

        return None


# Reverse of _MIC_TO_OUR_EXCHANGE — our exchange → canonical MIC
_OUR_EXCHANGE_TO_MIC: dict[str, str] = {v: k for k, v in _MIC_TO_OUR_EXCHANGE.items()}
# Add common aliases
_OUR_EXCHANGE_TO_MIC.update({
    "LSE":    "XLON",
    "NASDAQ": "XNAS",
    "NYSE":   "XNYS",
    "XETR":   "XETR",
    "US":     "XNYS",  # loose fallback
})
