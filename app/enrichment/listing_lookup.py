"""Candidate lookup is advisory; identity verification is separate from symbol guessing."""
import datetime
from decimal import Decimal
import re

from app.valuation import currency_code
from app.enrichment.ticker_mapper import build_yf_ticker_candidates

SYMBOL = re.compile(r"^[A-Za-z0-9^][A-Za-z0-9.^=\-/]{0,39}$")
ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
MIC = {"LSE": "XLON", "LONDON": "XLON", "NYSE": "XNYS", "NYQ": "XNYS",
       "NASDAQ": "XNAS", "NMS": "XNAS", "NGM": "XNAS", "NCM": "XNAS",
       "AMS": "XAMS", "PAR": "XPAR", "GER": "XETR",
       "MCE": "XMAD", "MADRID": "XMAD", "XMAD": "XMAD",
       "ASX": "XASX", "MIL": "XMIL", "MILAN": "XMIL",
       "STO": "XSTO", "STOCKHOLM": "XSTO"}
FIGI_VENUES = {"XLON": {"LN"}, "XNYS": {"UN"}, "XNAS": {"UW"},
               "XAMS": {"NA"}, "XPAR": {"FP"}, "XETR": {"GY"}, "XMAD": {"SM"},
               "XASX": {"AU"}, "XMIL": {"IM"}, "XSTO": {"SS"}}
SUFFIXES = {"XLON": ".L", "XAMS": ".AS", "XPAR": ".PA", "XETR": ".DE", "XMAD": ".MC",
            "XASX": ".AX", "XMIL": ".MI", "XSTO": ".ST"}


def figi_identity_match(symbol, mic, matches):
    suffix = SUFFIXES.get(mic, "")
    root = symbol[:-len(suffix)] if suffix and symbol.endswith(suffix) else symbol
    for item in matches:
        figi_ticker = str(item.get('ticker') or '')
        # OpenFIGI represents a terminal London ticker dot with '/' (UU/),
        # while Yahoo's venue suffix yields UU.L. Internal share-class slashes
        # remain significant and retain the existing '-' conversion (BT/A).
        if mic == 'XLON' and figi_ticker.endswith('/'):
            figi_ticker = figi_ticker[:-1]
        actual, expected = figi_ticker.replace('/', '-'), root
        # Swedish A/B classes are concatenated by OpenFIGI (HMB) and hyphenated
        # by Yahoo (HM-B.ST). Preserve the class letter, ISIN and venue checks.
        if mic == 'XSTO':
            if re.fullmatch(r'[A-Z0-9]+-[AB]', actual):
                actual = actual[:-2] + actual[-1]
            if re.fullmatch(r'[A-Z0-9]+-[AB]', expected):
                expected = expected[:-2] + expected[-1]
        if item.get("exchCode") in FIGI_VENUES.get(mic, set()) and actual == expected and item.get("figi"):
            return item
    return None


def compatible_currency(left, right):
    left, right = currency_code(left), currency_code(right)
    return bool(left and right) and (left == right or {left, right} <= {"GBP", "GBX"})


def assess_candidate(expected_isin, expected_currency, expected_mic, candidate):
    evidence = []
    if not ISIN.fullmatch(expected_isin or ""):
        evidence.append("Holding has no valid ISIN; re-import its identity before applying a mapping.")
    elif candidate.get("isin") != expected_isin:
        evidence.append("Candidate ISIN is missing or does not match the holding.")
    if not expected_currency:
        evidence.append("Holding price units are missing; re-import the source statement first.")
    elif not compatible_currency(expected_currency, candidate.get("currency")) and not candidate.get("allow_currency_conversion"):
        evidence.append("Quote currency is incompatible with the holding's recorded price units.")
    if not candidate.get("exchange"):
        evidence.append("Candidate exchange could not be verified.")
    if expected_mic and candidate.get("mic") != expected_mic:
        evidence.append("Candidate venue differs from the holding's recorded MIC.")
    try:
        price = Decimal(str(candidate.get("price")))
        if not price.is_finite() or price < 0 or not candidate.get("observed_at"):
            raise ValueError()
    except (ValueError, ArithmeticError):
        evidence.append("No usable quote with a timestamp was returned.")
    return {**candidate, "verified": not evidence,
            "evidence": evidence or ["Exact ISIN match", "Compatible quote currency/units", "Exchange and timestamped quote returned by provider"]}


class YahooListingLookup:
    def __init__(self):
        self._figi_cache = {}

    def figi_matches(self, isin):
        if isin not in self._figi_cache:
            import requests
            from app.config import settings
            headers = {"X-OPENFIGI-APIKEY": settings.openfigi_api_key} if settings.openfigi_api_key else {}
            response = requests.post("https://api.openfigi.com/v3/mapping", json=[
                {"idType": "ID_ISIN", "idValue": isin}], headers=headers, timeout=10)
            response.raise_for_status()
            data = response.json()
            self._figi_cache[isin] = data[0].get("data", []) if isinstance(data, list) and data else []
        return self._figi_cache[isin]

    def candidates(self, instrument, symbol=None):
        if symbol:
            if not SYMBOL.fullmatch(symbol):
                raise ValueError("Enter a Yahoo symbol, not a URL or description")
            return [symbol]
        if not ISIN.fullmatch(instrument.isin or ""):
            return []
        import yfinance as yf
        candidates = []
        try:
            candidates.extend(q.get("symbol") for q in yf.Search(
                instrument.isin, max_results=4, news_count=0, timeout=8).quotes)
        except Exception:
            pass
        # Hints are never accepted as identity evidence; every candidate is checked.
        base = instrument.ticker.split("_")[0]
        if instrument.currency_code in {"GBP", "GBX", "GBp"} and "_" not in instrument.ticker:
            candidates.append(base.replace("/", "-") + ".L")
        candidates.extend(build_yf_ticker_candidates(instrument.ticker, instrument.short_name, instrument.exchange))
        candidates.append(instrument.yf_ticker)
        return list(dict.fromkeys(c for c in candidates if c and SYMBOL.fullmatch(c)))[:4]

    def inspect(self, symbol, expected_isin=None):
        if not SYMBOL.fullmatch(symbol):
            raise ValueError("Invalid provider symbol")
        import yfinance as yf
        ticker = yf.Ticker(symbol)
        info = ticker.get_info() or {}
        direct_isin = info.get('isin')
        isin = direct_isin
        if not ISIN.fullmatch(isin or ''):
            try:
                isin = ticker.get_isin()
            except Exception:
                isin = None
        currency = currency_code(info.get("currency"))
        exchange = info.get("fullExchangeName") or info.get("exchange")
        code = str(info.get("exchange") or exchange or "").upper()
        mic = MIC.get(code)
        identity_evidence = {"source": "Yahoo ISIN"} if ISIN.fullmatch(isin or "") else None
        # get_isin() is a secondary symbol/name lookup and can pick a same-name
        # depositary receipt (AVGO -> Canadian DR). An exact OpenFIGI ISIN +
        # quote-venue + ticker match may resolve that inferred conflict. Never
        # override a conflicting ISIN supplied in direct instrument metadata.
        if (not ISIN.fullmatch(direct_isin or '') and ISIN.fullmatch(expected_isin or '')
                and isin != expected_isin):
            try:
                match = figi_identity_match(symbol, mic, self.figi_matches(expected_isin))
                if match:
                    inferred_isin = isin
                    isin = expected_isin
                    identity_evidence = {"source": "OpenFIGI ISIN/venue/ticker match",
                                         "requested_isin": expected_isin, "figi": match["figi"],
                                         "ticker": match["ticker"], "exchange_code": match["exchCode"]}
                    if ISIN.fullmatch(inferred_isin or '') and inferred_isin != expected_isin:
                        identity_evidence['conflicting_secondary_isin'] = inferred_isin
                        identity_evidence['secondary_source'] = 'Yahoo get_isin lookup'
            except Exception:
                pass
        history = ticker.history(period="5d", auto_adjust=False, timeout=8, raise_errors=True)
        close = history["Close"].dropna()
        if close.empty:
            raise ValueError("No recent provider quote")
        observed = close.index[-1].to_pydatetime()
        if observed.tzinfo:
            observed = observed.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        return dict(symbol=symbol, name=info.get("longName") or info.get("shortName") or symbol,
                    isin=isin if ISIN.fullmatch(isin or "") else None, currency=currency,
                    exchange=exchange, mic=mic, identity_evidence=identity_evidence,
                    price=str(Decimal(str(float(close.iloc[-1])))),
                    observed_at=observed.isoformat(), checked_at=datetime.datetime.utcnow().isoformat(),
                    provider="Yahoo Finance", quote_type="Latest available daily quote")
