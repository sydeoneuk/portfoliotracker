"""Read-only grouping of owned broker aliases with verified security identities."""
from collections import defaultdict
from sqlalchemy import and_, or_, text
from types import SimpleNamespace
from app.models import Instrument, Security, ProviderListingMapping, InstrumentListingLink, AccountListingResolution, AccountListingAudit, UserInstrumentMapping
from app.enrichment.listing_lookup import compatible_currency
from app import scoped_identity, listing_identity


def holding_resolver(db, user_id, tickers, *, batch=False):
    # Include sold accounts and historical aliases as well as current positions.
    observed = db.execute(text("""SELECT ticker,platform_id FROM positions WHERE user_id=:u
        UNION SELECT ticker,platform_id FROM orders WHERE user_id=:u
        UNION SELECT ticker,platform_id FROM dividend_payments WHERE user_id=:u
        UNION SELECT ticker,platform_id FROM holding_snapshots WHERE user_id=:u"""), {'u': user_id}).all()
    positions = [SimpleNamespace(ticker=symbol, platform_id=pid) for symbol, pid in observed if symbol]
    instruments = {i.ticker: i for i in db.query(Instrument).filter(
        Instrument.ticker.in_({p.ticker for p in positions} | set(tickers))).all()}
    resolutions = {(r.platform_id, r.ticker, r.source_namespace): r for r in
                   db.query(AccountListingResolution).filter_by(user_id=user_id).all()}
    claims_by_account = defaultdict(set)
    for resolution in resolutions.values():
        claims_by_account[(resolution.platform_id, resolution.ticker)].add(resolution.claim.get('isin'))
    positions_by_ticker = defaultdict(list)
    candidates_by_isin = defaultdict(list)
    for position in positions:
        positions_by_ticker[position.ticker].append(position)
        claims = claims_by_account[(position.platform_id, position.ticker)] | {instruments[position.ticker].isin}
        for isin in claims - {None, ''}:
            candidates_by_isin[isin].append(position)
    namespaces = dict(db.execute(text("""SELECT pl.id, 'holdings:' || COALESCE(b.source,'legacy')
        FROM platforms pl LEFT JOIN import_authorities a ON a.platform_id=pl.id AND a.user_id=pl.user_id AND a.scope='account'
        LEFT JOIN ingestion_batches b ON b.id=a.batch_id WHERE pl.user_id=:u"""), {'u': user_id}).all())
    mappings = {m.ticker: m for m in db.query(UserInstrumentMapping).filter_by(user_id=user_id).all()}
    references = [m.evidence for m in mappings.values() if m.ticker in instruments
                  and listing_identity.checked_candidate(instruments[m.ticker], m.evidence)['verified']]
    removed = {(r.platform_id, r.ticker, r.source_namespace) for r in
               db.query(AccountListingAudit).filter_by(user_id=user_id).all()}
    retained_namespaces = {}
    for pid, symbol, source in db.execute(text(scoped_identity.REFS +
            'SELECT platform_id,ticker,namespace FROM scopes WHERE user_id=:u'), {'u': user_id}):
        retained_namespaces.setdefault((pid, symbol), set()).add(source)
    cached = {}

    def verified_isin(position):
        key = (position.platform_id, position.ticker)
        if key in cached:
            return cached[key]
        instrument = instruments[position.ticker]
        if position.platform_id is None:
            return None
        namespace = namespaces.get(position.platform_id, 'holdings:legacy')
        resolution = resolutions.get((*key, namespace))
        isin = None
        claim = None
        provider_evidence = None
        if resolution:
            listing = scoped_identity.resolve_listing(db, position.platform_id, namespace, position.ticker)
            if listing:
                isin = db.get(Security, listing.security_id).isin
                claim = dict(isin=isin, mic=listing.mic, currency='GBX' if float(listing.unit_scale) == .01 else listing.quote_currency)
                provider_evidence = resolution.evidence['provider']
        else:
            if (*key, namespace) in removed:
                cached[key] = None
                return None
            # Never bypass a stale scoped match. Personal/global matches must
            # still agree with current metadata and retained account evidence.
            listing = listing_identity.resolve_listing(db, position.ticker)
            if listing is None and db.get(InstrumentListingLink, position.ticker) is not None:
                cached[key] = None
                return None
            if listing:
                isin = db.get(Security, listing.security_id).isin
                claim = dict(isin=isin, mic=listing.mic, currency='GBX' if float(listing.unit_scale) == .01 else listing.quote_currency)
                provider_evidence = db.query(ProviderListingMapping).filter_by(listing_id=listing.id, provider='yahoo').first()
                provider_evidence = provider_evidence.evidence if provider_evidence else None
            mapping = mappings.get(position.ticker)
            if not isin and mapping:
                checked = listing_identity.checked_candidate(instrument, mapping.evidence)
                if checked['verified'] and checked['isin'] == mapping.isin:
                    isin = mapping.isin
                    claim = mapping.evidence
            if not isin:
                # Reuse this owner's independently verified provider reference
                # for another broker alias declaring that exact ISIN and symbol.
                # This does not promote a catalogue ISIN or a name match alone.
                for reference in references:
                    if (instrument.isin == reference.get('isin') and instrument.yf_ticker == reference.get('symbol')
                            and listing_identity.checked_candidate(instrument, reference)['verified']):
                        isin = reference['isin']
                        claim = reference
                        break
        if isin:
            for source in retained_namespaces.get(key, {namespace}):
                claims, refs, problems = scoped_identity.source_claims(db, position.platform_id, source, position.ticker)
                if (problems or any(value != isin for value in claims['isin'])
                        or any(value != claim['mic'] for value in claims['mic'])
                        or any(not compatible_currency(value, claim['currency']) for value in claims['currency'])):
                    isin = None
                    break
        if isin and provider_evidence:
            references.append(provider_evidence)
        cached[key] = isin
        return isin

    if batch:
        # Collect verified references before grouping so input row order cannot
        # decide whether an equivalent alias sees a reference.
        for ticker in set(tickers):
            for position in positions_by_ticker[ticker]:
                verified_isin(position)
        for key in [key for key, isin in cached.items() if isin is None]:
            del cached[key]

    def resolve(ticker, platform_ids=None):
        anchors = [p for p in positions_by_ticker[ticker]
                   if platform_ids is None or p.platform_id in platform_ids]
        anchor_isins = {isin for p in anchors if (isin := verified_isin(p))}
        pairs = set()
        if len(anchor_isins) == 1:
            isin = next(iter(anchor_isins))
            for position in candidates_by_isin[isin]:
                if verified_isin(position) == isin:
                    pairs.add((position.ticker, position.platform_id))
        aliases = {ticker} | {symbol for symbol, pid in pairs}
        return sorted(pairs), instruments, aliases

    return resolve


def holding_scope(db, user_id, ticker, platform_ids=None):
    return holding_resolver(db, user_id, [ticker])(ticker, platform_ids)


def event_scope(model, ticker, pairs):
    return or_(model.ticker == ticker, *[
        and_(model.ticker == symbol, model.platform_id == pid) for symbol, pid in pairs])


def history_scope(alias, ticker, pairs):
    params = {'holding_ticker': ticker}
    clauses = [f'{alias}.ticker=:holding_ticker']
    for index, (symbol, pid) in enumerate(pairs):
        params[f'holding_alias_{index}'], params[f'holding_account_{index}'] = symbol, pid
        clauses.append(f'({alias}.ticker=:holding_alias_{index} AND {alias}.platform_id=:holding_account_{index})')
    return '(' + ' OR '.join(clauses) + ')', params


def group_display_holdings(db, user_id, rows, display_currency):
    """Group presentation rows after their monetary values have been converted."""
    if len(rows) < 2:
        return rows
    resolve = holding_resolver(db, user_id, [row['ticker'] for row in rows], batch=True)
    groups = {}
    for index, row in enumerate(rows):
        ticker = row['ticker']
        accounts = row.get('platform_ids') or []
        scope, _, aliases = resolve(ticker, accounts)
        verified = set(scope)
        if len(aliases) > 1 and accounts and all((ticker, pid) in verified for pid in accounts):
            key = tuple(scope)
        else:
            key = index
        groups.setdefault(key, []).append(row)

    result = []
    for members in groups.values():
        if len(members) == 1:
            result.append(members[0])
            continue
        row = dict(members[0])
        row['currency'] = display_currency
        row['quantity'] = sum(float(member['quantity'] or 0) for member in members)
        row['holding_accounts'] = sorted(
            {badge['id']: badge for member in members for badge in member['holding_accounts']}.values(),
            key=lambda badge: (badge['label'], badge['id']),
        )
        for field in ('value_display', 'cost_display', 'dividends_display', 'forward_dividends_display'):
            values = [member.get(field) for member in members]
            row[field] = sum(values) if all(value is not None for value in values) else None
        cost = row['cost_display']
        value = row['value_display']
        dividends = row['dividends_display']
        profit = value - cost if value is not None and cost is not None else None
        row['ppl_display'] = profit
        row['total_pnl_display'] = profit + dividends if profit is not None and dividends is not None else None
        for field, numerator in (
            ('cap_pnl_pct_display', profit), ('total_pnl_pct_display', row['total_pnl_display']),
            ('div_return_pct_display', dividends), ('fwd_yield_display', row['forward_dividends_display']),
        ):
            row[field] = numerator / cost * 100 if cost and numerator is not None else None
        # Per-share fundamentals and pie counts belong to individual source rows.
        for field in ('annual_rate_per_share', 'fcf_per_share_3y_avg', 'eps_ttm', 'pie_count'):
            row[field] = None
        result.append(row)
    return sorted(result, key=lambda row: row['value_display'] or 0, reverse=True)
