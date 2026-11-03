"""Reviewable, user-scoped price mapping corrections; no holding or cost rewrites."""
import datetime
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import or_
from app.models import Instrument, Position
from app.models.instrument_mapping import UserInstrumentMapping, InstrumentMappingProposal, InstrumentMappingAudit
from app.enrichment.listing_lookup import YahooListingLookup, assess_candidate
from app.enrichment.listing_lookup import ISIN
from app.valuation import convert_amount, get_fx_rates_to_gbp, ValuationUnavailable


class ReconciliationConflict(ValueError):
    pass


def metadata_snapshot(instrument):
    return {key: getattr(instrument, key) for key in ('isin', 'currency_code', 'mic_code', 'yf_ticker', 'exchange')}


def require_exclusive_instrument(db, user_id, ticker):
    # Metadata belongs to the shared catalogue. Never change another user's
    # historical records, even when they no longer hold a positive position.
    from app.models import Base
    for table in Base.metadata.sorted_tables:
        if 'ticker' in table.c and 'user_id' in table.c:
            foreign = db.query(table.c.ticker).filter(table.c.ticker == ticker,
                or_(table.c.user_id != user_id, table.c.user_id.is_(None))).first()
            if foreign:
                raise ReconciliationConflict('This instrument has shared or unassigned records. A catalogue-wide reconciliation is required; no metadata was changed.')


def propose_metadata(db, user_id, ticker, symbol, isin, currency, statement_reference, lookup=None):
    instrument = held_instrument(db, user_id, ticker)
    require_exclusive_instrument(db, user_id, ticker)
    isin, currency = isin.strip().upper(), currency.strip()
    from app.valuation import currency_code
    currency = currency_code(currency if currency == 'GBp' else currency.upper())
    if not ISIN.fullmatch(isin) or not statement_reference.strip():
        raise ValueError('Enter the statement ISIN and a statement/date reference')
    if currency not in {'GBP', 'GBX', 'USD', 'EUR', 'CAD', 'AUD', 'CHF', 'JPY', 'HKD', 'SGD', 'NZD', 'SEK', 'NOK', 'DKK', 'ZAR'}:
        raise ValueError('Enter a supported statement price currency (GBP and GBX are different units)')
    if instrument.currency_code and currency_code(instrument.currency_code) != currency:
        raise ValueError('Existing recorded price units cannot be changed in this workflow; reconcile historical amounts separately')
    original = baseline(instrument, mapping_for(db, user_id, ticker))
    before = metadata_snapshot(instrument)
    target = {**before, 'isin': isin, 'currency_code': currency}
    if target == before and symbol.strip() == (instrument.yf_ticker or ''):
        raise ValueError('No metadata change entered. Use Verify entered symbol instead.')
    lookup = lookup or YahooListingLookup()
    symbols = lookup.candidates(instrument, symbol.strip())
    if len(symbols) != 1:
        raise ValueError('Enter one provider symbol')
    try:
        candidate = prepare_candidate(SimpleNamespace(**target), lookup.inspect(symbols[0], expected_isin=isin))
    except Exception:
        raise ValueError('Provider verification unavailable; no metadata was changed')
    candidate['metadata_correction'] = {'before': before, 'after': target,
                                         'statement_reference': statement_reference.strip()}
    if candidate['verified']:
        target.update(yf_ticker=candidate['symbol'], exchange=candidate['exchange'])
    db.expire_all()
    instrument = held_instrument(db, user_id, ticker)
    if baseline(instrument, mapping_for(db, user_id, ticker)) != original:
        raise ReconciliationConflict('Holding changed during verification; review again')
    now = datetime.datetime.utcnow()
    proposal = InstrumentMappingProposal(user_id=user_id, ticker=ticker,
        status='verified' if candidate['verified'] else 'needs_review', baseline=original,
        candidate=candidate, created_at=now, expires_at=now + datetime.timedelta(minutes=30))
    db.add(proposal)
    db.commit()
    return [proposal]


def held_instrument(db, user_id, ticker):
    instrument = db.query(Instrument).join(Position, Position.ticker == Instrument.ticker).filter(
        Position.user_id == user_id, Position.quantity > 0, Instrument.ticker == ticker).first()
    if instrument is None:
        raise LookupError("Holding not found")
    return instrument


def mapping_for(db, user_id, ticker):
    return db.query(UserInstrumentMapping).filter_by(user_id=user_id, ticker=ticker).populate_existing().first()


def baseline(instrument, mapping):
    return dict(isin=instrument.isin, currency=instrument.currency_code, symbol=instrument.yf_ticker,
                exchange=instrument.exchange, mic=instrument.mic_code, revision=mapping.revision if mapping else 0)


FIELDS = ("isin", "listing_currency", "provider_symbol", "quote_currency", "exchange", "evidence",
          "price", "normalized_price", "conversion_rate", "price_observed_at", "checked_at", "revision")


def prepare_candidate(instrument, candidate, allow_currency_conversion=False):
    candidate = assess_candidate(instrument.isin, instrument.currency_code, instrument.mic_code,
                                 {**candidate, "allow_currency_conversion": allow_currency_conversion})
    if candidate["verified"]:
        try:
            rate = convert_amount(1, candidate["currency"], instrument.currency_code,
                                  get_fx_rates_to_gbp({candidate["currency"], instrument.currency_code}))
            candidate["conversion_rate"] = str(rate)
            candidate["normalized_price"] = str(Decimal(candidate["price"]) * Decimal(str(rate)))
            candidate["display_units"] = instrument.currency_code
        except ValuationUnavailable:
            candidate["verified"] = False
            candidate["evidence"] = ["Required exchange rate unavailable; no mapping can be applied yet."]
    return candidate


def snapshot(mapping):
    if mapping is None:
        return None
    result = {}
    for field in FIELDS:
        value = getattr(mapping, field)
        result[field] = value.isoformat() if isinstance(value, datetime.datetime) else str(value) if isinstance(value, Decimal) else value
    return result


def propose(db, user_id, ticker, symbol=None, lookup=None, allow_currency_conversion=False):
    instrument = held_instrument(db, user_id, ticker)
    original = baseline(instrument, mapping_for(db, user_id, ticker))
    lookup = lookup or YahooListingLookup()
    candidates = lookup.candidates(instrument, symbol)
    checked = []
    for candidate_symbol in candidates:
        try:
            candidate = prepare_candidate(instrument, lookup.inspect(candidate_symbol, expected_isin=instrument.isin),
                                          bool(symbol and allow_currency_conversion))
        except Exception:
            candidate = dict(symbol=candidate_symbol, verified=False,
                             evidence=["Provider lookup failed. Retry, or check the symbol against your statement."])
        checked.append(candidate)
    if not checked:
        checked = [dict(symbol=None, verified=False, evidence=["No candidates found. Supply a valid ISIN and listing currency from your statement."])]
    # A symbol guess is never sufficient. Multiple identity matches require an
    # explicit listing choice, checked again through the manual-symbol path.
    ambiguous = not symbol and sum(c.get("verified", False) for c in checked) > 1
    now = datetime.datetime.utcnow()
    # Recheck after the network lookup, rather than saving stale baseline evidence.
    db.expire_all()
    instrument = held_instrument(db, user_id, ticker)
    if baseline(instrument, mapping_for(db, user_id, ticker)) != original:
        raise ReconciliationConflict("Holding identity changed during lookup; check again")
    db.query(InstrumentMappingProposal).filter(
        InstrumentMappingProposal.user_id == user_id, InstrumentMappingProposal.ticker == ticker,
        InstrumentMappingProposal.status.in_(["verified", "ambiguous", "needs_review"]),
    ).update({"status": "superseded"}, synchronize_session=False)
    proposals = []
    for candidate in checked:
        status = "ambiguous" if ambiguous and candidate.get("verified") else "verified" if candidate.get("verified") else "needs_review"
        proposal = InstrumentMappingProposal(user_id=user_id, ticker=ticker, status=status,
                                             baseline=original, candidate=candidate,
                                             created_at=now, expires_at=now + datetime.timedelta(minutes=30))
        db.add(proposal)
        proposals.append(proposal)
    db.commit()
    return proposals


def lock_instrument(db, ticker):
    # Serialize changes even when there is no user mapping row yet.
    return db.query(Instrument).filter_by(ticker=ticker).populate_existing().with_for_update().one()


def apply_proposal(db, user_id, proposal_id):
    proposal = db.query(InstrumentMappingProposal).filter_by(id=proposal_id, user_id=user_id).first()
    if proposal is None:
        raise LookupError("Proposal not found")
    instrument = lock_instrument(db, proposal.ticker)
    held_instrument(db, user_id, proposal.ticker)
    db.refresh(proposal)
    mapping = mapping_for(db, user_id, proposal.ticker)
    if proposal.status != "verified" or proposal.expires_at < datetime.datetime.utcnow():
        raise ReconciliationConflict("Proposal is not applicable or has expired; verify the listing again")
    if proposal.baseline != baseline(instrument, mapping):
        raise ReconciliationConflict("Mapping or holding identity changed; verify the listing again")
    correction = proposal.candidate.get('metadata_correction')
    target = instrument
    if correction:
        require_exclusive_instrument(db, user_id, proposal.ticker)
        if metadata_snapshot(instrument) != correction['before']:
            raise ReconciliationConflict('Source metadata changed; review again')
        target = SimpleNamespace(**correction['after'])
    candidate = assess_candidate(target.isin, target.currency_code, target.mic_code, proposal.candidate)
    if not candidate["verified"]:
        raise ReconciliationConflict("Identity evidence no longer verifies this listing")
    before = snapshot(mapping)
    if correction:
        for field, value in correction['after'].items():
            setattr(instrument, field, value)
    revision = mapping.revision + 1 if mapping else 1
    if mapping is None:
        mapping = UserInstrumentMapping(user_id=user_id, ticker=proposal.ticker)
        db.add(mapping)
    mapping.isin, mapping.listing_currency = instrument.isin, instrument.currency_code
    mapping.provider_symbol, mapping.quote_currency = candidate["symbol"], candidate["currency"]
    mapping.exchange, mapping.evidence = candidate["exchange"], candidate
    mapping.price = Decimal(candidate["price"])
    mapping.normalized_price = Decimal(candidate["normalized_price"])
    mapping.conversion_rate = Decimal(candidate["conversion_rate"])
    mapping.price_observed_at = datetime.datetime.fromisoformat(candidate["observed_at"])
    mapping.checked_at, mapping.revision = datetime.datetime.utcnow(), revision
    audit = InstrumentMappingAudit(user_id=user_id, ticker=proposal.ticker, proposal_id=proposal.id,
                                   action="apply", before=before, after=snapshot(mapping))
    if correction:
        audit.after = {**audit.after, 'metadata_correction': correction}
    db.add(audit)
    proposal.status = "applied"
    db.commit()
    return mapping, audit


def undo_change(db, user_id, audit_id):
    audit = db.query(InstrumentMappingAudit).filter_by(id=audit_id, user_id=user_id, action="apply").first()
    if audit is None:
        raise LookupError("Mapping change not found")
    instrument = lock_instrument(db, audit.ticker)
    mapping = mapping_for(db, user_id, audit.ticker)
    undone = db.query(InstrumentMappingAudit).filter_by(undo_of=audit.id, user_id=user_id).first()
    if undone or mapping is None or mapping.revision != audit.after["revision"]:
        raise ReconciliationConflict("This is not the latest active correction; a newer change must not be overwritten")
    correction = audit.after.get('metadata_correction')
    if correction:
        require_exclusive_instrument(db, user_id, audit.ticker)
        if metadata_snapshot(instrument) != correction['after']:
            raise ReconciliationConflict('Catalogue metadata changed since this correction; undo would overwrite newer data')
        for field, value in correction['before'].items():
            setattr(instrument, field, value)
    before = snapshot(mapping)
    if audit.before is None:
        db.delete(mapping)
        restored = None
    else:
        for field in FIELDS:
            value = audit.before[field]
            if field in {"price_observed_at", "checked_at"} and value:
                value = datetime.datetime.fromisoformat(value)
            if field in {"price", "normalized_price", "conversion_rate"} and value is not None:
                value = Decimal(value)
            setattr(mapping, field, value)
        mapping.revision = before["revision"] + 1
        restored = snapshot(mapping)
    reversal = InstrumentMappingAudit(user_id=user_id, ticker=audit.ticker, action="undo",
                                      before=before, after=restored, undo_of=audit.id)
    db.add(reversal)
    db.commit()
    return reversal


def refresh_mapping(db, user_id, mapping_id, lookup=None):
    mapping = db.query(UserInstrumentMapping).filter_by(id=mapping_id, user_id=user_id).first()
    if mapping is None:
        raise LookupError("Mapping not found")
    instrument = held_instrument(db, user_id, mapping.ticker)
    symbol, revision = mapping.provider_symbol, mapping.revision
    if instrument.isin != mapping.isin or instrument.currency_code != mapping.listing_currency:
        raise ReconciliationConflict("Holding identity changed; verify a new mapping before refreshing")
    candidate = prepare_candidate(instrument, (lookup or YahooListingLookup()).inspect(symbol, expected_isin=mapping.isin),
                                  mapping.evidence.get("allow_currency_conversion", False))
    if not candidate["verified"]:
        raise ReconciliationConflict("Quote identity or units could not be verified; the previous quote is retained")
    instrument = lock_instrument(db, mapping.ticker)
    db.refresh(mapping)
    if mapping.revision != revision or mapping.provider_symbol != symbol:
        raise ReconciliationConflict("Mapping changed during price refresh; retry")
    if instrument.isin != mapping.isin or instrument.currency_code != mapping.listing_currency:
        raise ReconciliationConflict("Holding identity changed during refresh; verify again")
    mapping.price = Decimal(candidate["price"])
    mapping.normalized_price = Decimal(candidate["normalized_price"])
    mapping.conversion_rate = Decimal(candidate["conversion_rate"])
    mapping.quote_currency = candidate["currency"]
    mapping.price_observed_at = datetime.datetime.fromisoformat(candidate["observed_at"])
    mapping.checked_at = datetime.datetime.utcnow()
    mapping.evidence = {**mapping.evidence, "latest_quote_verification": candidate}
    db.commit()
    return mapping


def refresh_due_mappings(db, user_id=None):
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(hours=24)
    query = db.query(UserInstrumentMapping).join(Position, (Position.ticker == UserInstrumentMapping.ticker) &
                                                (Position.user_id == UserInstrumentMapping.user_id)).filter(
        Position.quantity > 0, UserInstrumentMapping.checked_at < cutoff)
    if user_id is not None:
        query = query.filter(UserInstrumentMapping.user_id == user_id)
    identities = {(m.user_id, m.id) for m in query.all()}
    updated, errors = 0, []
    for owner, mapping_id in sorted(identities):
        try:
            refresh_mapping(db, owner, mapping_id)
            updated += 1
        except Exception:
            db.rollback()
            errors.append(f"Mapping {mapping_id}: refresh could not be verified; previous quote retained")
    return {"fetched": updated, "errors": errors}
