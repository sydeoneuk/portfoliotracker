"""Fetch explicit quotes for verified holdings scopes, independently of imports."""
import datetime as dt
from decimal import Decimal
from types import SimpleNamespace
from sqlalchemy import text
from app.account_lock import account_write_lock
from app import scoped_identity as identity
from app.models import AccountListingPrice, AccountListingResolution, Instrument, ImportAuthority, ProviderListingMapping
from app.enrichment.listing_lookup import YahooListingLookup
from app.valuation import currency_code, convert_amount, get_fx_rates_to_gbp

QUOTE_TTL = dt.timedelta(hours=24)
MAX_QUOTE_AGE = dt.timedelta(days=7)


def authority_id(db, platform_id):
    authority = db.get(ImportAuthority, (platform_id, 'account'))
    return authority.batch_id if authority else None


def refresh_resolution(db, user_id, resolution_id, lookup=None, force=False):
    row = db.query(AccountListingResolution).filter_by(id=resolution_id, user_id=user_id).first()
    if row is None:
        raise LookupError('Account match not found')
    account_id, ticker, namespace = row.platform_id, row.ticker, row.source_namespace
    if not namespace.startswith('holdings:'):
        return {'status': 'unavailable', 'reason': 'A transaction or pie match cannot price account holdings without a holdings match.'}
    ctx = identity.context(db, account_id, namespace, ticker)
    listing = identity.resolve_listing(db, account_id, namespace, ticker)
    if not ctx['active'] or listing is None:
        return {'status': 'unavailable', 'reason': 'The holdings match needs review because its source evidence changed.'}
    before = identity.snapshot(row)
    metadata = identity.catalog.source_metadata(db.get(Instrument, ticker))
    batch_id = authority_id(db, account_id)
    now = dt.datetime.utcnow()
    cached = db.get(AccountListingPrice, row.id)
    if not force and cached and cached.expires_at and cached.expires_at > now and (
        cached.resolution_revision == row.revision and cached.source_fingerprint == ctx['fingerprint']
        and cached.catalogue_metadata == metadata and cached.authority_batch_id == batch_id
    ):
        return {'status': 'reused', 'reason': 'Using the current verified account quote.'}
    provider = db.get(ProviderListingMapping, row.provider_mapping_id)
    if provider.provider != 'yahoo' or provider.namespace != 'symbol':
        return {'status': 'unavailable', 'reason': 'The verified quote provider is unsupported.'}
    target = SimpleNamespace(isin=row.claim['isin'], currency_code=row.claim['currency'], mic_code=row.claim['mic'])
    error, quote = None, None
    try:
        try:
            candidate = (lookup or YahooListingLookup()).inspect(provider.identifier, expected_isin=target.isin)
        except Exception as exc:
            raise ValueError('The price provider is unavailable. Retry refreshing prices.') from exc
        checked = identity.catalog.checked_candidate(target, candidate)
        if not checked['verified'] or candidate['symbol'] != provider.identifier:
            raise ValueError('The provider quote no longer verifies this investment; review the match.')
        raw_currency = currency_code(candidate['currency'])
        base, scale = ('GBP', Decimal('.01')) if raw_currency == 'GBX' else (raw_currency, Decimal(1))
        if base != listing.quote_currency or scale != listing.unit_scale or candidate['mic'] != listing.mic:
            raise ValueError('The provider quote currency, units or exchange changed; review the match.')
        observed = dt.datetime.fromisoformat(candidate['observed_at'])
        if observed.tzinfo:
            observed = observed.astimezone(dt.timezone.utc).replace(tzinfo=None)
        if observed > now + dt.timedelta(minutes=5) or observed < now - MAX_QUOTE_AGE:
            raise ValueError('The provider quote is too old or has an invalid observation time.')
        # GBP is a display denomination for a newly verified quote, never an
        # assumption about units of legacy prices, costs or other stored amounts.
        normalized_currency = currency_code(metadata['currency_code']) or 'GBP'
        rates = get_fx_rates_to_gbp({raw_currency, normalized_currency})
        conversion = Decimal(str(convert_amount(1, raw_currency, normalized_currency, rates)))
        raw_price = Decimal(str(candidate['price']))
        normalized = raw_price * conversion
        if not conversion.is_finite() or conversion <= 0 or not normalized.is_finite() or not 0 <= normalized < Decimal('1e18'):
            raise ValueError('The converted quote is invalid.')
        quote = dict(raw_price=raw_price, quote_currency=raw_currency, unit_scale=scale,
            normalized_price=normalized, normalized_currency=normalized_currency, conversion_rate=conversion,
            observed_at=observed, fetched_at=now, expires_at=min(now + QUOTE_TTL, observed + MAX_QUOTE_AGE))
    except (ValueError, ArithmeticError) as exc:
        error = str(exc)[:500]
    with account_write_lock(db, account_id):
        db.expire_all()
        fresh = identity.mapping(db, account_id, namespace, ticker)
        if identity.snapshot(fresh) != before or identity.context(db, account_id, namespace, ticker)['fingerprint'] != ctx['fingerprint'] or authority_id(db, account_id) != batch_id:
            raise ValueError('The account match changed during price refresh. Retry.')
        cached = db.get(AccountListingPrice, fresh.id)
        if cached is None:
            cached = AccountListingPrice(resolution_id=fresh.id, user_id=user_id,
                resolution_revision=fresh.revision, source_fingerprint=ctx['fingerprint'],
                catalogue_metadata=metadata, authority_batch_id=batch_id)
            db.add(cached)
        if quote:
            for key, value in quote.items():
                setattr(cached, key, value)
            cached.resolution_revision, cached.source_fingerprint = fresh.revision, ctx['fingerprint']
            cached.catalogue_metadata, cached.authority_batch_id = metadata, batch_id
        cached.checked_at, cached.error = now, error
        db.commit()
    return {'status': 'fetched' if quote else 'unavailable',
            'reason': 'Verified account quote refreshed.' if quote else error}


def refresh_due_prices(db, user_id=None):
    from app.automatic_matching import match_unresolved_holdings, CachedLookup
    lookup = CachedLookup(YahooListingLookup())
    automatic = match_unresolved_holdings(db, user_id, lookup)
    # Historical/event/pie matches must not trigger unrelated network work.
    query = '''SELECT ar.id,ar.user_id FROM account_listing_resolutions ar
        JOIN positions p ON p.platform_id=ar.platform_id AND p.user_id=ar.user_id AND p.ticker=ar.ticker
        JOIN platforms pl ON pl.id=p.platform_id AND pl.user_id=p.user_id AND pl.is_active
        LEFT JOIN import_authorities aa ON aa.platform_id=ar.platform_id AND aa.user_id=ar.user_id AND aa.scope='account'
        LEFT JOIN ingestion_batches ab ON ab.id=aa.batch_id
        WHERE p.quantity>0 AND ar.source_namespace='holdings:' || COALESCE(ab.source,'legacy')'''
    params = {}
    if user_id is not None:
        query += ' AND ar.user_id=:u'
        params['u'] = user_id
    rows = db.execute(text(query), params).all()
    result = {'fetched': 0, 'errors': [], 'automatically_matched': automatic['matched'],
              'needs_attention': automatic['needs_attention']}
    for resolution_id, owner_id in rows:
        try:
            item = refresh_resolution(db, owner_id, resolution_id, lookup=lookup)
            if item['status'] == 'fetched':
                result['fetched'] += 1
            elif item['status'] == 'unavailable':
                result['errors'].append(f'Account match #{resolution_id}: {item["reason"]}')
        except Exception:
            db.rollback()
            result['errors'].append(f'Account match #{resolution_id}: price refresh failed; retry later.')
    return result


def refresh_import_price(db, user_id, batch_id, ticker):
    from app.import_matching import batch_for, namespace_for
    batch = batch_for(db, user_id, batch_id)
    namespace = namespace_for(db, batch)
    row = identity.mapping(db, batch.platform_id, namespace, ticker)
    if row is None:
        return {'status': 'unavailable', 'reason': 'No verified account match is available.'}
    return refresh_resolution(db, user_id, row.id)


def decorate_valuation_issues(db, user_id, rows, platform_ids=None):
    """Attach actionable explanations only to unavailable current holdings."""
    from app.models import Position, IngestionBatch
    missing = {row['ticker'] for row in rows if row.get('value') is None}
    if not missing:
        return rows
    positions = db.query(Position).filter(Position.user_id == user_id, Position.quantity > 0, Position.ticker.in_(missing))
    if platform_ids is not None:
        positions = positions.filter(Position.platform_id.in_(platform_ids))
    reasons = {}
    for position in positions:
        batch_id = authority_id(db, position.platform_id)
        batch = db.get(IngestionBatch, batch_id) if batch_id else None
        namespace = 'holdings:' + (batch.source if batch else 'legacy')
        resolution = identity.mapping(db, position.platform_id, namespace, position.ticker)
        if resolution is None:
            continue
        cached = db.get(AccountListingPrice, resolution.id)
        if identity.resolve_listing(db, position.platform_id, namespace, position.ticker) is None:
            reason = 'The verified holdings match needs review because its source evidence changed.'
        elif cached and cached.error:
            reason = cached.error
        else:
            reason = 'Verified account quote missing or expired. Refresh prices for this account.'
        reasons.setdefault(position.ticker, set()).add(reason)
    for row in rows:
        if row['ticker'] in reasons:
            row['valuation_issue'] = ' '.join(sorted(reasons[row['ticker']]))
    return rows
