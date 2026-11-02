"""Owner-scoped review of currently held, unpriced investments."""
from types import SimpleNamespace
from sqlalchemy import text
from app import scoped_identity as identity
from app.models import Platform, Position, Instrument, ImportAuthority, IngestionBatch
from app.enrichment.listing_lookup import YahooListingLookup
from app.valuation import effective_price_sql


def scope(db, user_id, platform_id, ticker):
    account = db.query(Platform).filter_by(id=platform_id, user_id=user_id, is_active=True).first()
    holding = db.query(Position).filter(Position.platform_id == platform_id, Position.user_id == user_id,
        Position.ticker == ticker, Position.quantity > 0).first()
    if account is None or holding is None:
        raise LookupError('Current holding not found')
    authority = db.get(ImportAuthority, (platform_id, 'account'))
    batch = db.get(IngestionBatch, authority.batch_id) if authority else None
    return account, 'holdings:' + (batch.source if batch else 'legacy')


def attention_list(db, user_id, search='', offset=0):
    where = f'''FROM positions p JOIN instruments i ON i.ticker=p.ticker
        JOIN platforms pl ON pl.id=p.platform_id AND pl.user_id=p.user_id
        WHERE p.user_id=:u AND p.quantity>0 AND pl.is_active AND ({effective_price_sql()}) IS NULL
        AND (p.ticker ILIKE :q OR i.name ILIKE :q OR pl.name ILIKE :q OR i.isin ILIKE :q)'''
    params = {'u': user_id, 'q': '%' + search[:80] + '%', 'offset': max(offset, 0)}
    total = db.execute(text('SELECT count(*) ' + where), params).scalar_one()
    rows = db.execute(text('SELECT p.ticker,p.platform_id,pl.name AS account_name,i.name ' + where +
        ' ORDER BY pl.name,p.platform_id,p.ticker LIMIT 25 OFFSET :offset'), params).mappings().all()
    items = []
    for row in rows:
        _, namespace = scope(db, user_id, row['platform_id'], row['ticker'])
        ctx = identity.context(db, row['platform_id'], namespace, row['ticker'])
        instrument = db.get(Instrument, row['ticker'])
        resolution = identity.mapping(db, row['platform_id'], namespace, row['ticker'])
        valid = resolution is not None and identity.resolve_listing(db, row['platform_id'], namespace, row['ticker']) is not None
        if ctx['problems']:
            reason = 'The retained source contains conflicting identity evidence. Re-import a corrected statement or contact support.'
        elif valid:
            reason = 'Your investment match is verified. Refresh its missing or expired price.'
        else:
            reason = 'Confirm the investment identity and quote units against your statement, then verify a pricing listing.'
        claim = resolution.claim if valid else {}
        items.append({**dict(row), 'reason': reason, 'can_review': not bool(ctx['problems']),
            'can_refresh': valid, 'isin': claim.get('isin') or (ctx['claims']['isin'][0] if len(ctx['claims']['isin']) == 1 else ''),
            'currency': claim.get('currency') or (ctx['claims']['currency'][0] if ctx['claims']['currency'] else ''),
            'mic': claim.get('mic') or (ctx['claims']['mic'][0] if len(ctx['claims']['mic']) == 1 else ''),
            'hints': {'isin': instrument.isin, 'currency': instrument.currency_code, 'symbol': instrument.yf_ticker}})
    return {'items': items, 'total': total, 'offset': max(offset, 0)}


def verify(db, user_id, platform_id, ticker, isin, currency, mic, statement_reference, symbol='', lookup=None):
    _, namespace = scope(db, user_id, platform_id, ticker)
    lookup = lookup or YahooListingLookup()
    # Validate the statement claim before any external requests.
    from app.valuation import currency_code
    unit = currency.strip()
    claim = {'isin': isin.strip().upper(), 'currency': currency_code(unit if unit == 'GBp' else unit.upper()),
             'mic': mic.strip().upper() or None, 'statement_reference': statement_reference.strip()}
    identity.check_claim(identity.context(db, platform_id, namespace, ticker), claim)
    if symbol.strip():
        return identity.verify(db, user_id, platform_id, namespace, ticker, symbol, lookup=lookup, **claim)
    target = SimpleNamespace(isin=claim['isin'], currency_code=claim['currency'], mic_code=claim['mic'],
        ticker=ticker, short_name=None, exchange=None, yf_ticker=None)
    try:
        symbols = list(dict.fromkeys(lookup.candidates(target)))
        if len(symbols) > 4:
            raise ValueError('Too many candidates')
        candidates = [identity.catalog.checked_candidate(target, lookup.inspect(s, expected_isin=claim['isin'])) for s in symbols]
    except Exception as exc:
        raise ValueError('Listing search is unavailable. Retry or enter a provider symbol from your statement.') from exc
    verified = [item for item in candidates if item['verified']]
    if len(verified) == 1:
        return identity.verify(db, user_id, platform_id, namespace, ticker, verified[0]['symbol'], lookup=lookup, **claim)
    return {'can_apply': False, 'review_token': None, 'candidates': verified,
            'message': 'Choose a compatible listing to verify.' if verified else 'No verified listing found. Check the statement details or enter a provider symbol.'}


def apply(db, user_id, token):
    try:
        review = identity.signer().loads(token)
        platform_id, namespace, ticker = review['scope']
    except Exception as exc:
        raise ValueError('The review is invalid. Verify the holding again.') from exc
    if review.get('actor') != user_id or review.get('owner') != user_id:
        raise LookupError('Review not found')
    _, current_namespace = scope(db, user_id, platform_id, ticker)
    if namespace != current_namespace:
        raise ValueError('The holdings source changed. Review the current statement again.')
    result = identity.apply(db, user_id, token)
    if identity.mapping(db, platform_id, namespace, ticker) is None:
        return {**result, 'pricing': {'status': 'unavailable', 'reason': 'This reviewed match was removed. Verify again to create a new match.'}}
    return {**result, 'pricing': refresh(db, user_id, platform_id, ticker)}


def refresh(db, user_id, platform_id, ticker, lookup=None):
    _, namespace = scope(db, user_id, platform_id, ticker)
    resolution = identity.mapping(db, platform_id, namespace, ticker)
    if resolution is None:
        raise ValueError('Verify a holding match before refreshing its price')
    from app.account_pricing import refresh_resolution
    try:
        return refresh_resolution(db, user_id, resolution.id, lookup=lookup)
    except Exception:
        db.rollback()
        return {'status': 'unavailable', 'reason': 'Match saved. Price refresh failed; retry refreshing this holding.'}
