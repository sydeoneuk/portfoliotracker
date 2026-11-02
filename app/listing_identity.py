"""Verified catalogue links; legacy holdings and prices remain unchanged."""
import datetime as dt
import hashlib
import json
import re
from decimal import Decimal
from itsdangerous import URLSafeTimedSerializer, BadSignature
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from app.config import settings
from app.models import Instrument, Security, Listing, ProviderListingMapping, InstrumentListingLink, ListingIdentityAudit
from app.enrichment.listing_lookup import YahooListingLookup, assess_candidate, SUFFIXES, SYMBOL
from app.valuation import currency_code

TTL = 900


def source_metadata(instrument):
    return {field: getattr(instrument, field) for field in ('isin', 'mic_code', 'currency_code', 'yf_ticker', 'exchange')}


def fingerprint(instrument):
    return hashlib.sha256(json.dumps(source_metadata(instrument), sort_keys=True).encode()).hexdigest()


def serializer():
    return URLSafeTimedSerializer(settings.session_secret, salt='stable-listing-review-v1')


def snapshot(link):
    if link is None:
        return None
    return {field: getattr(link, field) for field in ('listing_id', 'provider_mapping_id', 'source_fingerprint', 'revision', 'evidence')}


def resolve_listing(db, ticker):
    """Compatibility resolver for future readers: unresolved/stale fails closed."""
    instrument = db.get(Instrument, ticker)
    link = db.get(InstrumentListingLink, ticker)
    if instrument is None or link is None or link.source_fingerprint != fingerprint(instrument):
        return None
    return db.get(Listing, link.listing_id)


def checked_candidate(instrument, candidate):
    result = assess_candidate(instrument.isin, instrument.currency_code, instrument.mic_code,
        {**candidate, 'allow_currency_conversion': False})
    if not re.fullmatch(r'[A-Z0-9]{4}', candidate.get('mic') or ''):
        result['verified'] = False
        result['evidence'] = [*result['evidence'], 'A verified listing MIC is required; ISIN alone cannot identify a listing.']
    if not SYMBOL.fullmatch(candidate.get('symbol') or ''):
        result['verified'] = False
        result['evidence'] = [*result['evidence'], 'A valid provider symbol is required.']
    if currency_code(candidate.get('currency')) not in {'GBP', 'GBX', 'USD', 'EUR', 'CAD', 'AUD', 'CHF', 'JPY', 'HKD', 'SGD', 'NZD', 'SEK', 'NOK', 'DKK', 'ZAR'}:
        result['verified'] = False
        result['evidence'] = [*result['evidence'], 'Quote currency/unit scale is not supported for stable identity yet.']
    return result


def verify(db, actor_id, ticker, symbol, lookup=None):
    symbol = (symbol or '').strip().upper()
    instrument = db.get(Instrument, ticker)
    if instrument is None:
        raise LookupError('Instrument not found')
    baseline = fingerprint(instrument)
    link = db.get(InstrumentListingLink, ticker)
    original = snapshot(link)
    lookup = lookup or YahooListingLookup()
    if not SYMBOL.fullmatch(symbol or ''):
        raise ValueError('Enter a Yahoo symbol, not a URL or description')
    try:
        candidate = checked_candidate(instrument, lookup.inspect(symbol, expected_isin=instrument.isin))
    except Exception as exc:
        raise ValueError('Provider verification unavailable; no identity records changed') from exc
    db.expire_all()
    instrument = db.get(Instrument, ticker)
    if fingerprint(instrument) != baseline or snapshot(db.get(InstrumentListingLink, ticker)) != original:
        raise ValueError('Catalogue changed during verification. Review again.')
    token = None
    if candidate['verified']:
        token = serializer().dumps({'actor': actor_id, 'ticker': ticker, 'baseline': baseline,
            'before': original, 'candidate': candidate})
    return {'ticker': ticker, 'source': source_metadata(instrument), 'candidate': candidate,
        'review_token': token, 'expires_in_seconds': TTL, 'can_apply': token is not None}


def ensure_verified_listing(db, candidate, isin):
    """Create/reuse only verified stable identities; caller owns the transaction."""
    # Serialize shared provider identities across different legacy aliases.
    db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {'key': 'listing:yahoo:' + candidate['symbol']})
    now = dt.datetime.utcnow()
    security_id = db.execute(insert(Security).values(isin=isin, created_at=now)
        .on_conflict_do_nothing(index_elements=['isin']).returning(Security.id)).scalar()
    if security_id is None:
        security_id = db.query(Security.id).filter_by(isin=isin).scalar()
    quote = currency_code(candidate['currency'])
    monetary_currency, scale = ('GBP', Decimal('.01')) if quote == 'GBX' else (quote, Decimal(1))
    suffix = SUFFIXES.get(candidate['mic'])
    venue_symbol = candidate['symbol'][:-len(suffix)] if suffix and candidate['symbol'].endswith(suffix) else candidate['symbol']
    values = dict(security_id=security_id, mic=candidate['mic'], quote_currency=monetary_currency,
                  unit_scale=scale, venue_symbol=venue_symbol)
    listing_id = db.execute(insert(Listing).values(**values, created_at=now).on_conflict_do_nothing(
        constraint='uq_listing_identity').returning(Listing.id)).scalar()
    if listing_id is None:
        listing_id = db.query(Listing.id).filter_by(**values).scalar()
    provider = db.query(ProviderListingMapping).filter_by(provider='yahoo', namespace='symbol', identifier=candidate['symbol']).first()
    if provider is not None and provider.listing_id != listing_id:
        raise ValueError('This provider symbol is already verified for a different listing. Reassignment requires a separate catalogue reconciliation.')
    if provider is None:
        provider = ProviderListingMapping(provider='yahoo', namespace='symbol', identifier=candidate['symbol'],
            listing_id=listing_id, evidence=candidate, verified_at=now)
        db.add(provider)
        db.flush()
    return security_id, listing_id, provider


def apply(db, actor_id, review_token):
    try:
        review = serializer().loads(review_token, max_age=TTL)
    except BadSignature as exc:
        raise ValueError('Review expired or is invalid. Verify again.') from exc
    if review.get('actor') != actor_id:
        raise LookupError('Review not found')
    instrument = db.query(Instrument).filter_by(ticker=review['ticker']).with_for_update().populate_existing().one_or_none()
    if instrument is None:
        raise LookupError('Instrument not found')
    link = db.query(InstrumentListingLink).filter_by(ticker=instrument.ticker).populate_existing().first()
    if fingerprint(instrument) != review['baseline'] or snapshot(link) != review['before']:
        raise ValueError('Catalogue or alias link changed since review. Verify again.')
    candidate = checked_candidate(instrument, review['candidate'])
    if not candidate['verified']:
        raise ValueError('This listing is not verified')
    security_id, listing_id, provider = ensure_verified_listing(db, candidate, instrument.isin)
    now = dt.datetime.utcnow()
    before = snapshot(link)
    if link is None:
        link = InstrumentListingLink(ticker=instrument.ticker, revision=0)
        db.add(link)
    link.listing_id, link.provider_mapping_id = listing_id, provider.id
    link.source_fingerprint, link.evidence, link.verified_at = fingerprint(instrument), candidate, now
    link.revision += 1
    audit = ListingIdentityAudit(user_id=actor_id, ticker=instrument.ticker, action='link', before=before, after=snapshot(link))
    db.add(audit)
    db.commit()
    return {'ticker': instrument.ticker, 'security_id': security_id, 'listing_id': listing_id, 'audit_id': audit.id}


def undo(db, actor_id, audit_id):
    audit = db.get(ListingIdentityAudit, audit_id)
    if audit is None or audit.action != 'link':
        raise LookupError('Link correction not found')
    instrument = db.query(Instrument).filter_by(ticker=audit.ticker).with_for_update().populate_existing().one_or_none()
    link = db.query(InstrumentListingLink).filter_by(ticker=audit.ticker).populate_existing().first()
    if instrument is None or link is None or snapshot(link) != audit.after or fingerprint(instrument) != link.source_fingerprint:
        raise ValueError('Alias or catalogue changed; undo cannot overwrite newer state')
    if db.query(ListingIdentityAudit).filter_by(undo_of=audit.id).first():
        raise ValueError('This change has already been undone')
    before = snapshot(link)
    if audit.before is None:
        db.delete(link)
        after = None
    else:
        for field, value in audit.before.items():
            setattr(link, field, value)
        link.revision = before['revision'] + 1
        link.verified_at = dt.datetime.utcnow()
        after = snapshot(link)
    db.add(ListingIdentityAudit(user_id=actor_id, ticker=audit.ticker, action='undo', before=before, after=after, undo_of=audit.id))
    db.commit()
    return {'ticker': audit.ticker, 'message': 'Previous alias link restored. Stable IDs and evidence retained.'}


def audit_report(db, search='', offset=0, limit=25):
    rows = db.query(Instrument).filter(Instrument.ticker.ilike('%' + search[:80] + '%') |
        Instrument.isin.ilike('%' + search[:80] + '%') | Instrument.name.ilike('%' + search[:80] + '%')).order_by(Instrument.ticker)
    total = rows.count()
    items = []
    for instrument in rows.offset(max(0, offset)).limit(min(max(limit, 1), 100)):
        link = db.get(InstrumentListingLink, instrument.ticker)
        status = 'unresolved' if link is None else 'verified' if link.source_fingerprint == fingerprint(instrument) else 'stale'
        linked_listing = db.get(Listing, link.listing_id) if link else None
        items.append({'ticker': instrument.ticker, 'name': instrument.name, **source_metadata(instrument),
            'status': status, 'listing_id': link.listing_id if link else None,
            'security_id': linked_listing.security_id if linked_listing else None,
            'quote_currency': linked_listing.quote_currency if linked_listing else None,
            'unit_scale': str(linked_listing.unit_scale) if linked_listing else None})
    return {'items': items, 'total': total, 'offset': max(0, offset)}


def audit_summary(db):
    counts = {'verified': 0, 'stale': 0, 'unresolved': 0}
    rows = db.query(Instrument, InstrumentListingLink).outerjoin(InstrumentListingLink,
        InstrumentListingLink.ticker == Instrument.ticker).yield_per(500)
    for instrument, link in rows:
        status = 'unresolved' if link is None else 'verified' if link.source_fingerprint == fingerprint(instrument) else 'stale'
        counts[status] += 1
    counts['candidate_alias_groups'] = db.execute(text('''SELECT COUNT(*) FROM (
        SELECT isin,mic_code,currency_code,yf_ticker FROM instruments
        WHERE isin ~ '^[A-Z]{2}[A-Z0-9]{9}[0-9]$' AND mic_code IS NOT NULL
          AND currency_code IS NOT NULL AND yf_ticker IS NOT NULL
        GROUP BY isin,mic_code,currency_code,yf_ticker HAVING COUNT(*) > 1) candidates''')).scalar_one()
    counts['securities'] = db.query(Security).count()
    counts['listings'] = db.query(Listing).count()
    return counts
