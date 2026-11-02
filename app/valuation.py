"""Shared native-price selection and explicit currency conversion.

SQL helpers only accept application-owned column expressions, never user input.
Stored GBP equivalents are deliberately not substituted for native prices.
"""
import math
import time
from dataclasses import dataclass
from decimal import Decimal


class ValuationUnavailable(ValueError):
    pass


_fx_cache: dict[str, tuple[float, float]] = {}
FX_TTL_SECONDS = 3600


def currency_code(currency: str | None) -> str:
    return "GBX" if currency in {"GBX", "GBp"} else (currency or "")


def required_rate(currency: str | None, rates: dict[str, float]) -> float:
    currency = currency_code(currency)
    if currency == "GBP":
        return 1.0
    if currency == "GBX":
        return 0.01
    rate = rates.get(currency)
    if rate is None or not math.isfinite(float(rate)) or float(rate) <= 0:
        raise ValuationUnavailable(f"Exchange rate unavailable for {currency or 'unknown currency'}")
    return float(rate)


def get_fx_rates_to_gbp(currencies: set[str]) -> dict[str, float]:
    rates = {"GBP": 1.0, "GBX": 0.01, "GBp": 0.01}
    needed = {currency_code(c) for c in currencies} - {"GBP", "GBX", "", "—", "-"}
    if not needed:
        return rates
    import yfinance as yf
    now = time.monotonic()
    for currency in needed:
        cached = _fx_cache.get(currency)
        if cached and now - cached[1] < FX_TTL_SECONDS:
            rates[currency] = cached[0]
            continue
        try:
            rate = float(yf.Ticker(f"{currency}GBP=X").fast_info["last_price"])
            if not math.isfinite(rate) or rate <= 0:
                continue
            rates[currency] = rate
            _fx_cache[currency] = (rate, now)
        except Exception:
            # An expired cached rate must not be silently treated as current.
            continue
    return rates


def convert_amount(value, source: str, target: str, rates: dict[str, float]) -> float:
    if value is None:
        raise ValuationUnavailable("Amount unavailable")
    source, target = currency_code(source), currency_code(target)
    if not source or not target or source in {"—", "-"} or target in {"—", "-"}:
        raise ValuationUnavailable("Currency unavailable")
    if source == target:
        return float(value)
    return float(Decimal(str(value)) * Decimal(str(required_rate(source, rates)))
                 / Decimal(str(required_rate(target, rates))))


@dataclass(frozen=True)
class HoldingValuation:
    quantity: Decimal
    price_native: Decimal
    currency: str
    rate_to_gbp: Decimal
    value_gbp: Decimal


def value_holding(quantity, price_native, currency: str, rates: dict[str, float]) -> HoldingValuation:
    if price_native is None:
        raise ValuationUnavailable("Market price unavailable")
    quantity, price = Decimal(str(quantity)), Decimal(str(price_native))
    if not quantity.is_finite() or not price.is_finite() or price < 0:
        raise ValuationUnavailable("Invalid quantity or market price")
    rate = Decimal(str(required_rate(currency, rates)))
    return HoldingValuation(quantity, price, currency_code(currency), rate, quantity * price * rate)


def _global_quote_price_sql(ticker: str) -> str:
    """Native listing-unit quote. Fresh sources outrank stale sources.

    Among fresh quotes retain provider priority; among stale quotes use the
    latest observation. GBP/pence conversion is explicit; other currency
    mismatches are rejected rather than using a differently denominated quote.
    """
    currency = f"(SELECT qi.currency_code FROM instruments qi WHERE qi.ticker = {ticker})"
    correction_time = metadata_correction_time_sql(ticker)
    return f"""(SELECT CASE
        WHEN ip.currency = {currency} THEN ip.price
        WHEN ip.currency = 'GBP' AND {currency} IN ('GBX', 'GBp') THEN ip.price * 100
        WHEN ip.currency IN ('GBX', 'GBp') AND {currency} = 'GBP' THEN ip.price / 100
        WHEN ip.currency IN ('GBX', 'GBp') AND {currency} IN ('GBX', 'GBp') THEN ip.price
        END
        FROM instrument_prices ip WHERE ip.ticker = {ticker}
        AND ip.price >= 0 AND ip.price < 'Infinity'::numeric
        AND ({correction_time} IS NULL OR (ip.source = 'yfinance' AND ip.updated_at >= {correction_time}))
        AND (ip.currency = {currency} OR
             (ip.currency IN ('GBP','GBX','GBp') AND {currency} IN ('GBP','GBX','GBp')))
        ORDER BY (ip.updated_at >= (CURRENT_TIMESTAMP AT TIME ZONE 'UTC') - INTERVAL '24 hours') DESC,
          CASE WHEN ip.updated_at >= (CURRENT_TIMESTAMP AT TIME ZONE 'UTC') - INTERVAL '24 hours'
            THEN CASE ip.source WHEN 't212_api' THEN 1 WHEN 'yfinance' THEN 2 ELSE 3 END END,
          ip.updated_at DESC, ip.source
        LIMIT 1)"""


def mapping_exists_sql(ticker, user_id):
    return f"EXISTS (SELECT 1 FROM user_instrument_mappings um WHERE um.ticker = {ticker} AND um.user_id = {user_id})"


def metadata_correction_time_sql(ticker):
    # Quotes captured before an identity/unit correction cannot be reinterpreted
    # under the new catalogue metadata, including after an undo.
    return f"""(SELECT MAX(ma.created_at) FROM instrument_mapping_audits ma
        WHERE ma.ticker = {ticker} AND (ma.after ? 'metadata_correction' OR EXISTS (
            SELECT 1 FROM instrument_mapping_audits original
            WHERE original.id = ma.undo_of AND original.after ? 'metadata_correction')))"""


def quote_price_sql(ticker: str, user_id=None) -> str:
    global_quote = _global_quote_price_sql(ticker)
    if user_id is None:
        return global_quote
    mapped_quote = f"""(SELECT um.normalized_price
        FROM user_instrument_mappings um JOIN instruments ui ON ui.ticker = um.ticker
        WHERE um.user_id = {user_id} AND um.ticker = {ticker}
          AND um.isin = ui.isin AND um.listing_currency = ui.currency_code
          AND um.price >= 0 AND um.price < 'Infinity'::numeric
          AND um.normalized_price >= 0 AND um.normalized_price < 'Infinity'::numeric
          AND um.conversion_rate > 0 AND um.conversion_rate < 'Infinity'::numeric)"""
    return f"(CASE WHEN {mapping_exists_sql(ticker, user_id)} THEN {mapped_quote} ELSE {global_quote} END)"


def account_price_scope_sql(ticker, user_id, platform_id):
    return f"""FROM account_listing_resolutions ar
        LEFT JOIN import_authorities aa ON aa.platform_id=ar.platform_id AND aa.user_id=ar.user_id AND aa.scope='account'
        LEFT JOIN ingestion_batches ab ON ab.id=aa.batch_id
        WHERE ar.ticker={ticker} AND ar.user_id={user_id} AND ar.platform_id={platform_id}
          AND ar.source_namespace='holdings:' || COALESCE(ab.source,'legacy')"""


def account_quote_valid_sql():
    return """aq.user_id=ar.user_id AND aq.resolution_revision=ar.revision
        AND aq.source_fingerprint=ar.source_fingerprint
        AND aq.authority_batch_id IS NOT DISTINCT FROM aa.batch_id
        AND aq.catalogue_metadata=jsonb_build_object('isin', ai.isin, 'mic_code', ai.mic_code,
            'currency_code', ai.currency_code, 'yf_ticker', ai.yf_ticker, 'exchange', ai.exchange)
        AND aq.normalized_currency=COALESCE(CASE WHEN ai.currency_code='GBp' THEN 'GBX' ELSE ai.currency_code END,'GBP')
        AND aq.expires_at > (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')
        AND aq.observed_at <= (CURRENT_TIMESTAMP AT TIME ZONE 'UTC') + INTERVAL '5 minutes'
        AND aq.observed_at >= (CURRENT_TIMESTAMP AT TIME ZONE 'UTC') - INTERVAL '7 days'
        AND aq.normalized_price >= 0 AND aq.normalized_price < 'Infinity'::numeric
        AND aq.conversion_rate > 0 AND aq.conversion_rate < 'Infinity'::numeric
        AND EXISTS(SELECT 1 FROM platforms ap WHERE ap.id=ar.platform_id AND ap.user_id=ar.user_id AND ap.is_active)"""


def account_price_sql(ticker, user_id, platform_id):
    scope = account_price_scope_sql(ticker, user_id, platform_id)
    return f"""(SELECT (SELECT aq.normalized_price FROM account_listing_prices aq
        JOIN instruments ai ON ai.ticker=ar.ticker WHERE aq.resolution_id=ar.id AND {account_quote_valid_sql()}) {scope})"""


def effective_price_sql(ticker="p.ticker", current="p.current_price", user_id=None, use_account_prices=True) -> str:
    # Position prices are broker observations in listing units. Cost is never
    # a substitute for a missing market price.
    user_id = user_id or current.rsplit(".", 1)[0] + ".user_id"
    quote = quote_price_sql(ticker, user_id)
    legacy = f"CASE WHEN {mapping_exists_sql(ticker, user_id)} THEN {quote} ELSE COALESCE({quote}, CASE WHEN {metadata_correction_time_sql(ticker)} IS NULL AND {current} >= 0 AND {current} < 'Infinity'::float THEN {current} END) END"
    if not use_account_prices:
        return legacy
    legacy = f"CASE WHEN (SELECT li.currency_code FROM instruments li WHERE li.ticker={ticker}) IS NOT NULL THEN ({legacy}) END"
    platform_id = current.rsplit('.', 1)[0] + '.platform_id'
    scope = account_price_scope_sql(ticker, user_id, platform_id)
    return f"CASE WHEN EXISTS(SELECT 1 {scope}) THEN {account_price_sql(ticker, user_id, platform_id)} ELSE ({legacy}) END"


def strict_sum_sql(expression: str) -> str:
    return f"CASE WHEN COUNT(*) FILTER (WHERE ({expression}) IS NULL) > 0 THEN NULL ELSE SUM({expression}) END"


def materialize_position_prices_sql(query: str) -> str:
    """Evaluate guarded prices once per owned position before aggregation.

    These replacements apply only to application-owned portfolio SQL using the
    established p/pos aliases. They happen before inserting the CTE so the scalar
    price selection itself still reads the real positions table. MATERIALIZED is
    essential: otherwise PostgreSQL can inline it and repeat expensive subplans.
    """
    import re
    query = str(query).strip()
    replaced = False
    for alias in ('p', 'pos'):
        expression = effective_price_sql(f'{alias}.ticker', f'{alias}.current_price')
        if expression in query:
            query = query.replace(expression, f'{alias}.valuation_price')
            replaced = True
    if not replaced:
        raise ValueError('Portfolio query has no supported position price expression')
    query, count = re.subn(r'\b(FROM|JOIN)\s+positions\s+(p|pos)\b',
                          r'\1 priced_positions \2', query, flags=re.IGNORECASE)
    if not count:
        raise ValueError('Portfolio query has no supported position relation')
    cte = f'''priced_positions AS MATERIALIZED (
        SELECT p.*, {effective_price_sql()} AS valuation_price
        FROM positions p WHERE p.user_id=:user_id
    )'''
    if query[:5].upper() == 'WITH ':
        return 'WITH ' + cte + ', ' + query[5:]
    return 'WITH ' + cte + ' ' + query


def execute_position_query(db, query, params):
    """Run a bounded portfolio projection without costly JIT compilation.

    The guarded scalar selectors inflate PostgreSQL's estimated cost enough to
    compile hundreds of functions for a few hundred rows. Measured compilation
    dominates execution here. Change JIT only for this query's transaction and
    restore the prior setting after success; rollback resets it after SQL errors.
    """
    from sqlalchemy import text
    statement = text(materialize_position_prices_sql(query))
    previous = db.execute(text('SHOW jit')).scalar_one()
    db.execute(text('SET LOCAL jit = off'))
    rows = db.execute(statement, params).fetchall()
    db.execute(text("SELECT set_config('jit', :previous, true)"), {'previous': previous})
    return rows


def split_priced_positions(positions, display_currency, rates):
    """Keep unavailable holdings visible without inventing values or totals."""
    priced, unavailable = [], []
    for original in positions:
        row = dict(original)
        try:
            if row.get("value") is None:
                raise ValuationUnavailable(row.get('valuation_issue') or "Market price unavailable")
            if not math.isfinite(float(row["value"])):
                raise ValuationUnavailable("Invalid market value")
            convert_amount(row["value"], row.get("currency"), display_currency, rates)
        except ValuationUnavailable as exc:
            row["valuation_issue"] = str(exc)
            unavailable.append(row)
        else:
            priced.append(row)
    return priced, unavailable
