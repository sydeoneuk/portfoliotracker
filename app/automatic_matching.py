"""Resolve current account identities by independently verifying an existing ISIN."""
import datetime as dt
import logging
from types import SimpleNamespace
from app import scoped_identity as identity
from app.enrichment.listing_lookup import YahooListingLookup, ISIN, MIC, SYMBOL, SUFFIXES, compatible_currency
from app.models import Instrument, AccountListingAudit
# Standalone refresh/CLI workers must register users before ORM FK sorting.
from app.auth import models as _auth_models

log = logging.getLogger(__name__)


class CachedLookup:
    """Per-refresh cache only: share public quote lookups, never account decisions."""
    def __init__(self, lookup=None):
        self.lookup = lookup or YahooListingLookup()
        self.searches, self.quotes = {}, {}

    def candidates(self, target):
        key = (target.isin, target.ticker, target.currency_code, target.yf_ticker, target.exchange)
        if key not in self.searches:
            self.searches[key] = self.lookup.candidates(target)
        return self.searches[key]

    def inspect(self, symbol, expected_isin=None):
        key = (symbol, expected_isin)
        if key not in self.quotes:
            try:
                self.quotes[key] = self.lookup.inspect(symbol, expected_isin=expected_isin)
            except Exception as exc:
                self.quotes[key] = exc
        value = self.quotes[key]
        if isinstance(value, Exception):
            raise value
        return dict(value)


def match_scope(db, user_id, account_id, namespace, ticker, lookup=None):
    result = {'ticker': ticker, 'status': 'needs_attention'}
    def attention(reason):
        return {**result, 'reason': reason}
    ctx = identity.context(db, account_id, namespace, ticker)
    if ctx['owner_id'] != user_id:
        raise LookupError('Holding not found')
    if not ctx['active'] or ctx['problems']:
        return attention('The account is archived or its retained identity evidence conflicts. Review the holding.')
    existing = identity.mapping(db, account_id, namespace, ticker)
    before = identity.snapshot(existing)
    if existing:
        if identity.resolve_listing(db, account_id, namespace, ticker) is not None:
            return {**result, 'status': 'reused', 'listing_id': existing.listing_id,
                    'reason': 'Reused the verified account match.'}
        return attention('The previous match no longer agrees with its source. Review the holding.')
    if db.query(AccountListingAudit).filter_by(platform_id=account_id, source_namespace=namespace, ticker=ticker).first():
        return attention('A previous match was removed. Review before matching it again.')
    instrument = db.get(Instrument, ticker)
    claims = ctx['claims']
    isin = claims['isin'][0] if len(claims['isin']) == 1 else (instrument.isin or '').strip().upper()
    if not ISIN.fullmatch(isin):
        return attention('No valid ISIN is available. Supply the statement ISIN for review.')
    aliases = db.query(Instrument).filter_by(isin=isin).order_by(Instrument.ticker).all()
    # Catalogue aliases provide discovery/venue preferences only. Every accepted
    # candidate must independently return the exact ISIN and explicit quote units.
    alias_mics = {i.mic_code or MIC.get((i.exchange or '').upper()) for i in aliases}
    alias_mics.discard(None)
    preferred_mic = claims['mic'][0] if claims['mic'] else (
        instrument.mic_code or MIC.get((instrument.exchange or '').upper()) or
        (next(iter(alias_mics)) if len(alias_mics) == 1 else None))
    preferred_currency = claims['currency'][0] if claims['currency'] else instrument.currency_code
    target = SimpleNamespace(isin=isin, currency_code=preferred_currency, mic_code=preferred_mic,
        ticker=ticker, short_name=instrument.short_name, exchange=instrument.exchange, yf_ticker=instrument.yf_ticker)
    lookup = lookup or CachedLookup()
    hints = sorted({i.yf_ticker for i in aliases if i.yf_ticker and SYMBOL.fullmatch(i.yf_ticker)},
                   key=lambda symbol: ('.' not in symbol, symbol))
    try:
        symbols = list(dict.fromkeys(hints + list(lookup.candidates(target))))
    except Exception:
        symbols = hints
    if not symbols:
        return attention('No pricing listing was found for this ISIN. Retry later or review a symbol.')
    if len(symbols) > 12:
        return attention('Too many potential listings were found. Choose a listing for review.')
    verified = []
    for symbol in symbols:
        try:
            candidate = lookup.inspect(symbol, expected_isin=isin)
            # Missing legacy units can be learned from a provider-verified quote.
            check = SimpleNamespace(isin=isin, currency_code=claims['currency'][0] if claims['currency'] else candidate.get('currency'),
                                    mic_code=claims['mic'][0] if claims['mic'] else None)
            candidate = identity.catalog.checked_candidate(check, candidate)
            if candidate['verified'] and candidate.get('symbol') == symbol and all(
                    compatible_currency(unit, candidate['currency']) for unit in claims['currency']):
                observed = dt.datetime.fromisoformat(candidate['observed_at'])
                if observed.tzinfo:
                    observed = observed.astimezone(dt.timezone.utc).replace(tzinfo=None)
                if dt.datetime.utcnow() - dt.timedelta(days=7) <= observed <= dt.datetime.utcnow() + dt.timedelta(minutes=5):
                    verified.append(candidate)
        except Exception:
            continue
    if preferred_mic:
        preferred = [item for item in verified if item['mic'] == preferred_mic]
        if preferred:
            verified = preferred
    # An exact broker trading-line symbol distinguishes currency lines sharing
    # one ISIN (USDV.L GBP versus UDVD.L USD). Do this only after provider identity
    # and retained MIC/units checks; never collapse different share-class symbols.
    broker_symbol = ticker.strip().upper()
    if SYMBOL.fullmatch(broker_symbol):
        exact = []
        for item in verified:
            suffix = SUFFIXES.get(item['mic'], '')
            root = item['symbol'][:-len(suffix)] if suffix and item['symbol'].endswith(suffix) else item['symbol']
            if root.upper() == broker_symbol or item['symbol'].upper() == broker_symbol:
                exact.append(item)
        if exact:
            verified = exact
    direct = [item for item in verified if item['symbol'] == instrument.yf_ticker]
    if direct:
        verified = direct
    if preferred_currency:
        preferred = [item for item in verified if compatible_currency(preferred_currency, item['currency'])]
        if preferred:
            verified = preferred
    if len(verified) != 1:
        return attention('Several verified listings need a choice.' if verified else
            'No listing could be independently verified. Retry later or review the identity details.')
    chosen = verified[0]
    db.expire_all()
    fresh = identity.context(db, account_id, namespace, ticker)
    if fresh['fingerprint'] != ctx['fingerprint'] or identity.snapshot(identity.mapping(db, account_id, namespace, ticker)) != before:
        return attention('The source or match changed during lookup. Retry matching.')
    try:
        review = identity.verify(db, user_id, account_id, namespace, ticker, chosen['symbol'], isin,
            chosen['currency'], chosen['mic'],
            f'Automatic provider validation of existing ISIN {isin} on {dt.date.today().isoformat()}', lookup=lookup)
        if not review['can_apply']:
            return attention('The provider could not verify this listing consistently. Review the holding.')
        saved = identity.apply(db, user_id, review['review_token'])
    except Exception as exc:
        db.rollback()
        # Keep financial records and raw source rows out of diagnostic messages.
        detail = str(exc) if isinstance(exc, (ValueError, LookupError)) else str(
            getattr(getattr(getattr(exc, 'orig', None), 'diag', None), 'message_primary', type(exc).__name__))
        log.warning('Automatic match save failed: %s', detail)
        return attention('The match could not be saved. Retry matching; financial records are unchanged.')
    return {**result, 'status': 'matched', 'listing_id': saved['listing_id'],
            'reason': 'Existing ISIN independently verified; account match saved automatically.'}


def match_holding(db, user_id, platform_id, ticker, lookup=None):
    from app.holding_attention import scope
    _, namespace = scope(db, user_id, platform_id, ticker)
    return match_scope(db, user_id, platform_id, namespace, ticker, lookup)


def match_unresolved_holdings(db, user_id=None, lookup=None):
    from sqlalchemy import text
    from app.valuation import effective_price_sql
    rows = db.execute(text(f'''SELECT p.platform_id,p.user_id,p.ticker FROM positions p
        JOIN platforms pl ON pl.id=p.platform_id AND pl.user_id=p.user_id
        WHERE p.quantity>0 AND pl.is_active AND ({effective_price_sql()}) IS NULL'''
        + (' AND p.user_id=:u' if user_id is not None else '') + ' ORDER BY p.platform_id,p.ticker'),
        {'u': user_id} if user_id is not None else {}).mappings().all()
    lookup = lookup or CachedLookup()
    counts = {'matched': 0, 'needs_attention': 0}
    for row in rows:
        try:
            result = match_holding(db, row['user_id'], row['platform_id'], row['ticker'], lookup)
            if result['status'] == 'matched':
                counts['matched'] += 1
            elif result['status'] == 'needs_attention':
                counts['needs_attention'] += 1
        except Exception:
            db.rollback()
            counts['needs_attention'] += 1
    return counts
