"""Freeze the inputs of a snapshot, never infer units for existing history."""
from decimal import Decimal
from app.valuation import account_price_scope_sql, account_quote_valid_sql, currency_code


def snapshot_quote_sql(alias):
    scope = account_price_scope_sql(f'{alias}.ticker', f'{alias}.user_id', f'{alias}.platform_id')
    evidence = f"""(SELECT (SELECT jsonb_build_object(
        'source', 'verified_account_quote', 'listing_id', ar.listing_id,
        'listing', (SELECT to_jsonb(sl) FROM listings sl WHERE sl.id=ar.listing_id),
        'resolution_id', ar.id, 'resolution_revision', ar.revision,
        'source_fingerprint', ar.source_fingerprint, 'authority_batch_id', aq.authority_batch_id,
        'provider_mapping_id', ar.provider_mapping_id,
        'raw_price', aq.raw_price, 'quote_currency', aq.quote_currency,
        'quote_unit_scale', aq.unit_scale, 'observed_at', aq.observed_at,
        'fetched_at', aq.fetched_at, 'normalized_currency', aq.normalized_currency,
        'conversion_rate', aq.conversion_rate)
        FROM account_listing_prices aq JOIN instruments ai ON ai.ticker=ar.ticker
        WHERE aq.resolution_id=ar.id AND {account_quote_valid_sql()}) {scope})"""
    return evidence


def snapshot_fields(row, valuation):
    currency = currency_code(valuation.currency)
    evidence = dict(getattr(row, 'quote_evidence', None) or {
        'source': 'legacy_effective_price', 'provider_identity': 'unknown',
    })
    evidence.update(calculation_version='snapshot-1', ticker=row.ticker,
                    quantity=str(valuation.quantity), price_native=str(valuation.price_native),
                    price_currency=currency, fx_rate_to_gbp=str(valuation.rate_to_gbp))
    return dict(price_currency=currency,
                price_unit_scale=Decimal('.01') if currency == 'GBX' else Decimal(1),
                fx_rate_to_gbp=valuation.rate_to_gbp, valuation_evidence=evidence)
