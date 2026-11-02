"""Account/source identity evidence and explicit, restartable scoped resolution."""
import csv
import datetime as dt
import hashlib
import io
import json
import uuid
from types import SimpleNamespace
from itsdangerous import URLSafeTimedSerializer, BadSignature
from sqlalchemy import text
from app.config import settings
from app.account_lock import account_write_lock
from app.models import (Platform, Instrument, IngestionBatch, ImportAuthority, Order, DividendPayment,
    AccountListingResolution, AccountListingAudit, Listing)
from app.models.order import TradeImportObservation
from app import listing_identity as catalog
from app.enrichment.listing_lookup import YahooListingLookup, ISIN, compatible_currency
from app.valuation import currency_code

TTL = 900
MAX_BATCH = 20

REFS = '''WITH refs AS (
 SELECT p.platform_id,p.user_id,p.ticker,
   'holdings:' || COALESCE(b.source,'legacy') AS namespace,'position' AS ref
 FROM positions p LEFT JOIN import_authorities a ON a.platform_id=p.platform_id AND a.user_id=p.user_id AND a.scope='account'
 LEFT JOIN ingestion_batches b ON b.id=a.batch_id
 UNION ALL SELECT platform_id,user_id,ticker,'events:' || source_namespace,'order:' || pk FROM orders WHERE ticker IS NOT NULL
 UNION ALL SELECT platform_id,user_id,ticker,'events:' || source_namespace,'dividend:' || id FROM dividend_payments
 UNION ALL SELECT p.platform_id,p.user_id,h.ticker,'pie:' || p.source_namespace,'pie:' || h.id
 FROM pie_holdings h JOIN pies p ON p.pk=h.pie_id AND p.user_id=h.user_id
), scopes AS (
 SELECT r.platform_id,r.user_id,r.ticker,r.namespace,COUNT(*) AS reference_count,
 EXISTS(SELECT 1 FROM positions hp WHERE hp.platform_id=r.platform_id AND hp.user_id=r.user_id AND hp.ticker=r.ticker AND hp.quantity>0) AS held
 FROM refs r JOIN platforms p ON p.id=r.platform_id AND p.user_id=r.user_id
 GROUP BY r.platform_id,r.user_id,r.ticker,r.namespace
) '''


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def signer():
    return URLSafeTimedSerializer(settings.session_secret, salt='account-listing-resolution-v1')


def mapping(db, account_id, namespace, ticker):
    return db.query(AccountListingResolution).filter_by(platform_id=account_id, source_namespace=namespace, ticker=ticker).populate_existing().first()


def snapshot(row):
    if row is None:
        return None
    return {field: getattr(row, field) for field in ('listing_id', 'provider_mapping_id', 'source_fingerprint', 'claim', 'evidence', 'revision')}


def source_claims(db, account_id, namespace, ticker):
    """Read identity fields only; settlement/cost currencies are never quote units."""
    values = {'isin': set(), 'currency': set(), 'mic': set()}
    references, problems = set(), []
    def collect(isin=None, currency=None, mic=None, ref=None):
        if ref:
            references.add(ref)
        if isin:
            isin = str(isin).strip().upper()
            if not ISIN.fullmatch(isin):
                problems.append('A retained source row has an invalid ISIN; reconcile that source before linking.')
            else:
                values['isin'].add(isin)
        if currency:
            unit = str(currency).strip()
            values['currency'].add(currency_code(unit if unit == 'GBp' else unit.upper()))
        if mic:
            values['mic'].add(str(mic).strip().upper())
    source = namespace.partition(':')[2]
    owner_id = db.get(Platform, account_id).user_id
    batches = db.query(IngestionBatch).filter_by(platform_id=account_id, user_id=owner_id, status='applied')
    if namespace.startswith('holdings:'):
        authority = db.get(ImportAuthority, (account_id, 'account'))
        batches = batches.filter(IngestionBatch.id == authority.batch_id) if authority else batches.filter(text('false'))
    else:
        sources = {'events:t212': ['t212_history'], 'events:ii': ['ii_transactions'], 'pie:t212_csv_pie': ['pie_snapshot']}.get(namespace, [])
        batches = batches.filter(IngestionBatch.source.in_(sources))
    for batch in batches.order_by(IngestionBatch.id).yield_per(50):
        if batch.source == 't212_api':
            # API positions do not supply reliable listing identity fields in
            # the retained flat schema. Preserve a fetch anchor, never guess.
            references.add('batch:' + str(batch.id) + ':' + batch.checksum)
            continue
        reader = csv.DictReader(io.StringIO(batch.raw_content.decode('utf-8-sig').lstrip('\ufeff')))
        for number, row in enumerate(reader, 2):
            clean = {str(key).strip().lstrip('\ufeff').lower(): str(value or '').strip() for key, value in row.items() if key is not None}
            symbol = clean.get('ticker') or clean.get('symbol') or clean.get('slice') or clean.get('code') or clean.get('epic')
            if symbol != ticker:
                continue
            ccy = clean.get('currency (price / share)')
            if batch.source == 'ii_holdings':
                from app.importers.ii_csv import _parse_ii_value
                _, ccy = _parse_ii_value(clean.get('price', ''))
            collect(clean.get('isin'), ccy, clean.get('mic') or clean.get('mic code'),
                f'batch:{batch.id}:row:{number}:{batch.checksum}')
    if namespace.startswith('events:'):
        orders = db.query(Order).filter_by(platform_id=account_id, user_id=owner_id, source_namespace=source, ticker=ticker)
        for order in orders:
            collect(currency=order.execution_currency, ref='order:' + str(order.pk))
        observations = db.query(TradeImportObservation).join(Order, Order.pk == TradeImportObservation.order_pk).filter(
            Order.platform_id == account_id, Order.user_id == owner_id, TradeImportObservation.user_id == owner_id,
            Order.source_namespace == source, Order.ticker == ticker)
        for observation in observations:
            payload = {str(k).strip().lstrip('\ufeff').lower(): v for k, v in observation.payload.items()}
            collect(payload.get('isin'), payload.get('currency (price / share)'), payload.get('mic'),
                'trade-observation:' + str(observation.id) + ':' + observation.payload_hash)
    return {key: sorted(items) for key, items in values.items()}, sorted(references), sorted(set(problems))


def context(db, account_id, namespace, ticker):
    account = db.get(Platform, account_id)
    if account is None:
        raise LookupError('Account not found')
    refs = db.execute(text(REFS + 'SELECT ref FROM refs WHERE platform_id=:p AND user_id=:u AND namespace=:n AND ticker=:t ORDER BY ref'),
        {'p': account_id, 'u': account.user_id, 'n': namespace, 't': ticker}).scalars().all()
    if not refs:
        raise LookupError('No records exist for this account/source/alias scope')
    claims, evidence_refs, problems = source_claims(db, account_id, namespace, ticker)
    if len(claims['isin']) > 1 or len(claims['mic']) > 1:
        problems.append('This scope contains conflicting identity claims. Per-record or dated reconciliation is required.')
    if any(not compatible_currency(left, right) for left in claims['currency'] for right in claims['currency']):
        problems.append('This scope contains incompatible quote currencies; it cannot be assigned one listing.')
    instrument = db.get(Instrument, ticker)
    identity = {'account': account_id, 'owner': account.user_id, 'namespace': namespace, 'ticker': ticker,
        'claims': claims, 'catalogue_revision_guard': catalog.source_metadata(instrument), 'active': account.is_active}
    return {'platform_id': account_id, 'owner_id': account.user_id, 'account_name': account.name,
        'namespace': namespace, 'ticker': ticker, 'claims': claims, 'problems': problems,
        'references': evidence_refs[:30], 'reference_count': len(refs), 'fingerprint': digest(identity), 'active': account.is_active}


def check_claim(ctx, claim):
    if not ctx['active']:
        raise ValueError('Archived accounts cannot receive new identity resolutions')
    if ctx['problems']:
        raise ValueError(' '.join(ctx['problems']))
    if not ISIN.fullmatch(claim['isin']) or not claim.get('currency') or not claim.get('statement_reference', '').strip():
        raise ValueError('Supply a valid statement ISIN, quote units and a dated statement reference')
    observed = ctx['claims']
    if observed['isin'] and claim['isin'] not in observed['isin']:
        raise ValueError('Statement ISIN contradicts the retained source; reconcile the source instead of overriding it')
    if observed['mic'] and claim.get('mic') and claim['mic'] not in observed['mic']:
        raise ValueError('Statement MIC contradicts the retained source')
    if any(not compatible_currency(claim['currency'], value) for value in observed['currency']):
        raise ValueError('Statement quote units contradict retained execution/quote currency; cost currency is separate')


def verify(db, actor_id, account_id, namespace, ticker, symbol, isin, currency, mic, statement_reference, lookup=None):
    ctx = context(db, account_id, namespace, ticker)
    original = snapshot(mapping(db, account_id, namespace, ticker))
    unit = currency.strip()
    claim = {'isin': isin.strip().upper(), 'currency': currency_code(unit if unit == 'GBp' else unit.upper()),
        'mic': (mic or '').strip().upper() or None, 'statement_reference': statement_reference.strip()}
    check_claim(ctx, claim)
    if not claim['mic'] and ctx['claims']['mic']:
        claim['mic'] = ctx['claims']['mic'][0]
    target = SimpleNamespace(isin=claim['isin'], currency_code=claim['currency'], mic_code=claim['mic'])
    from app.enrichment.listing_lookup import SYMBOL
    symbol = symbol.strip().upper()
    if not SYMBOL.fullmatch(symbol):
        raise ValueError('Enter a provider symbol, not a URL or description')
    try:
        candidate = catalog.checked_candidate(target, (lookup or YahooListingLookup()).inspect(symbol, expected_isin=claim['isin']))
    except Exception as exc:
        raise ValueError('Provider verification unavailable; no resolution changed') from exc
    db.expire_all()
    fresh = context(db, account_id, namespace, ticker)
    if fresh['fingerprint'] != ctx['fingerprint'] or snapshot(mapping(db, account_id, namespace, ticker)) != original:
        raise ValueError('Source or resolution changed during verification. Review again.')
    token = None
    if candidate['verified']:
        claim['mic'] = candidate['mic']
        token = signer().dumps({'actor': actor_id, 'key': str(uuid.uuid4()), 'scope': [account_id, namespace, ticker],
            'owner': ctx['owner_id'], 'fingerprint': ctx['fingerprint'], 'before': original, 'claim': claim, 'candidate': candidate})
    return {'context': ctx, 'claim': claim, 'candidate': candidate, 'review_token': token, 'can_apply': token is not None, 'expires_in_seconds': TTL}


def apply(db, actor_id, token):
    try:
        review = signer().loads(token)
    except BadSignature as exc:
        raise ValueError('Review expired or is invalid; verify again') from exc
    if review.get('actor') != actor_id:
        raise LookupError('Review not found')
    account_id, namespace, ticker = review['scope']
    with account_write_lock(db, account_id):
        db.expire_all()
        previous = db.query(AccountListingAudit).filter_by(review_key=review['key']).first()
        if previous:
            return {'status': 'already_processed', 'audit_id': previous.id, 'platform_id': account_id, 'namespace': namespace, 'ticker': ticker}
        try:
            signer().loads(token, max_age=TTL)
        except BadSignature as exc:
            raise ValueError('Review expired; verify again. Already committed items are retained.') from exc
        ctx = context(db, account_id, namespace, ticker)
        current = mapping(db, account_id, namespace, ticker)
        if ctx['owner_id'] != review['owner'] or ctx['fingerprint'] != review['fingerprint'] or snapshot(current) != review['before']:
            raise ValueError('Source or mapping changed since review. Verify again.')
        check_claim(ctx, review['claim'])
        target = SimpleNamespace(isin=review['claim']['isin'], currency_code=review['claim']['currency'], mic_code=review['claim']['mic'])
        candidate = catalog.checked_candidate(target, review['candidate'])
        if not candidate['verified']:
            raise ValueError('Provider evidence does not verify this scope')
        security_id, listing_id, provider = catalog.ensure_verified_listing(db, candidate, target.isin)
        before = snapshot(current)
        if current is None:
            current = AccountListingResolution(user_id=ctx['owner_id'], platform_id=account_id,
                source_namespace=namespace, ticker=ticker, revision=0)
            db.add(current)
        current.listing_id, current.provider_mapping_id = listing_id, provider.id
        current.source_fingerprint, current.claim = ctx['fingerprint'], review['claim']
        current.evidence = {'provider': candidate, 'source_references': ctx['references']}
        current.revision += 1
        current.verified_at = dt.datetime.utcnow()
        audit = AccountListingAudit(user_id=ctx['owner_id'], actor_id=actor_id, platform_id=account_id,
            source_namespace=namespace, ticker=ticker, action='apply', review_key=review['key'], before=before, after=snapshot(current))
        db.add(audit)
        db.commit()
        return {'status': 'applied', 'platform_id': account_id, 'namespace': namespace, 'ticker': ticker,
            'listing_id': listing_id, 'security_id': security_id, 'audit_id': audit.id}


def apply_batch(db, actor_id, tokens):
    if not 1 <= len(tokens) <= MAX_BATCH:
        raise ValueError(f'Apply between one and {MAX_BATCH} reviewed scopes at a time')
    results = []
    for number, token in enumerate(tokens):
        try:
            result = apply(db, actor_id, token)
        except (ValueError, LookupError) as exc:
            db.rollback()
            result = {'status': 'blocked', 'error': str(exc)}
        except Exception:
            db.rollback()
            result = {'status': 'failed', 'error': 'Database application failed; this item was rolled back. Previously committed items are retained.'}
        results.append({'item': number, **result})
    return {'items': results}


def undo(db, actor_id, audit_id):
    audit = db.get(AccountListingAudit, audit_id)
    if audit is None or audit.action != 'apply':
        raise LookupError('Resolution correction not found')
    with account_write_lock(db, audit.platform_id):
        db.expire_all()
        current = mapping(db, audit.platform_id, audit.source_namespace, audit.ticker)
        ctx = context(db, audit.platform_id, audit.source_namespace, audit.ticker)
        if current is None or snapshot(current) != audit.after or ctx['fingerprint'] != current.source_fingerprint:
            raise ValueError('Source or resolution changed; undo cannot overwrite newer state')
        if db.query(AccountListingAudit).filter_by(undo_of=audit.id).first():
            raise ValueError('This correction was already undone')
        before = snapshot(current)
        if audit.before is None:
            db.delete(current)
            after = None
        else:
            for key, value in audit.before.items():
                setattr(current, key, value)
            current.revision = before['revision'] + 1
            current.verified_at = dt.datetime.utcnow()
            after = snapshot(current)
        db.add(AccountListingAudit(user_id=audit.user_id, actor_id=actor_id, platform_id=audit.platform_id,
            source_namespace=audit.source_namespace, ticker=audit.ticker, action='undo', before=before, after=after, undo_of=audit.id))
        db.commit()
        return {'message': 'Previous scoped resolution restored; source and financial records are unchanged'}


def resolve_listing(db, account_id, namespace, ticker):
    row = mapping(db, account_id, namespace, ticker)
    if row is None:
        return None
    try:
        ctx = context(db, account_id, namespace, ticker)
    except LookupError:
        return None
    if row.source_fingerprint != ctx['fingerprint'] or ctx['problems']:
        return None
    return db.get(Listing, row.listing_id)


def audit_report(db, search='', offset=0, held_only=True):
    params = {'q': '%' + search[:80] + '%', 'offset': max(offset, 0), 'held': held_only}
    where = ''' FROM scopes s JOIN platforms p ON p.id=s.platform_id JOIN instruments i ON i.ticker=s.ticker
        WHERE (NOT :held OR s.held) AND (s.ticker ILIKE :q OR p.name ILIKE :q OR i.isin ILIKE :q)'''
    total = db.execute(text(REFS + 'SELECT COUNT(*)' + where), params).scalar_one()
    rows = db.execute(text(REFS + 'SELECT s.*' + where + ' ORDER BY s.platform_id,s.ticker,s.namespace LIMIT 25 OFFSET :offset'), params).mappings().all()
    items = []
    contexts = {}
    def read_context(account_id, namespace, ticker):
        key = (account_id, namespace, ticker)
        if key not in contexts:
            contexts[key] = context(db, account_id, namespace, ticker)
        return contexts[key]
    for row in rows:
        ctx = read_context(row['platform_id'], row['namespace'], row['ticker'])
        resolution = mapping(db, row['platform_id'], row['namespace'], row['ticker'])
        instrument = db.get(Instrument, row['ticker'])
        peers = db.execute(text(REFS + 'SELECT platform_id,namespace FROM scopes WHERE ticker=:t'), {'t': row['ticker']}).mappings().all()
        peer_isins, peer_mics, peer_currencies = set(), set(), set()
        for peer in peers:
            pc = read_context(peer['platform_id'], peer['namespace'], row['ticker'])
            peer_isins.update(pc['claims']['isin']); peer_mics.update(pc['claims']['mic']); peer_currencies.update(pc['claims']['currency'])
            pr = mapping(db, peer['platform_id'], peer['namespace'], row['ticker'])
            if pr and pr.source_fingerprint == pc['fingerprint']:
                peer_isins.add(pr.claim['isin']); peer_mics.add(pr.claim['mic']); peer_currencies.add(pr.claim['currency'])
        cross_conflict = len(peer_isins) > 1 or len(peer_mics) > 1 or any(not compatible_currency(a,b) for a in peer_currencies for b in peer_currencies)
        status = 'conflict' if ctx['problems'] else 'unresolved' if resolution is None else 'verified' if resolution.source_fingerprint == ctx['fingerprint'] else 'stale'
        hints = db.query(Instrument.ticker).filter(Instrument.isin == instrument.isin, Instrument.ticker != instrument.ticker).limit(10).all() if instrument.isin else []
        items.append({**ctx, 'status': status, 'resolution': snapshot(resolution), 'legacy_alias_conflict': cross_conflict,
            'catalogue_hint': catalog.source_metadata(instrument), 'other_alias_hints': [value[0] for value in hints]})
    unassigned = db.execute(text('''SELECT (SELECT count(*) FROM orders WHERE platform_id IS NULL OR user_id IS NULL)
        + (SELECT count(*) FROM dividend_payments WHERE platform_id IS NULL OR user_id IS NULL)''')).scalar_one()
    return {'items': items, 'total': total, 'offset': params['offset'], 'unassigned_event_records': unassigned}
