"""Resolve legacy cash-event scope only from an exact authenticated API match."""
import datetime as dt
import hashlib
import json
from decimal import Decimal
from sqlalchemy import or_, and_
from app.models import Transaction, Platform, IngestionBatch
from app.auth import models as _auth_models  # register user FKs for standalone workers
from app.identity import T212


def utc_datetime(value):
    try:
        if isinstance(value, str):
            value = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        if not isinstance(value, dt.datetime):
            return None
        return value.astimezone(dt.timezone.utc).replace(tzinfo=None) if value.tzinfo else value
    except (ValueError, TypeError):
        return None


def same_event(row, item, external_id):
    legacy_interest = row.type == 'DEPOSIT' and item.get('type') == 'INTEREST_ON_FREE_CASH'
    if not row.type or not item.get('type') or (row.type != item['type'] and not legacy_interest):
        return False
    before, observed = utc_datetime(row.date_time), utc_datetime(item.get('dateTime'))
    if before is None or observed is None or before != observed:
        return False
    try:
        old, new = Decimal(str(row.amount)), Decimal(str(item.get('amount')))
        if not old.is_finite() or not new.is_finite() or old != new:
            return False
        if legacy_interest and old < 0:
            return False
    except (ValueError, ArithmeticError):
        return False
    if row.reference and str(row.reference) not in {external_id, str(item.get('reference') or '')}:
        return False
    return True


def reconcile_transaction(db, user_id, platform_id, item, audit_entries=None):
    """No commits: scope update, evidence and normal upsert share the sync transaction.

    'deferred' means retain all old rows and skip this incoming event, avoiding a
    duplicate while permitting unrelated sync work. CSV imports keep their strict
    unresolved-identity guard; this path requires a known authenticated API account.
    """
    external_id = str(item.get('reference') or item.get('id') or '')
    if not external_id:
        raise ValueError('Transaction has no provider identifier; sync rolled back')
    candidates = db.query(Transaction).filter(
        Transaction.user_id == user_id, Transaction.id == external_id,
        or_(Transaction.platform_id.is_(None), and_(Transaction.platform_id == platform_id,
            Transaction.source_namespace == 'legacy'))).with_for_update().all()
    if not candidates:
        return {'status': 'none'}
    account = db.query(Platform).filter_by(id=platform_id, user_id=user_id, platform_type='t212_api').first()
    if account is None or any(row.source_namespace != 'legacy' for row in candidates):
        return {'status': 'deferred', 'reason': 'Unverified legacy source or account'}
    matching = [row for row in candidates if same_event(row, item, external_id)]
    if len(matching) != 1:
        return {'status': 'deferred', 'reason': 'Legacy event details are incomplete, conflicting or duplicated'}
    current = db.query(Transaction).filter_by(user_id=user_id, platform_id=platform_id,
        source_namespace=T212, id=external_id).first()
    if current is not None:
        return {'status': 'deferred', 'reason': 'A scoped copy already exists; legacy copy retained for review'}
    row = matching[0]
    before = {column.name: getattr(row, column.name) for column in Transaction.__table__.columns}
    after = {**before, 'platform_id': platform_id, 'source_namespace': T212}
    # Prove the receipt can be retained before making the assignment.
    entry = json.loads(json.dumps({'record_pk': row.pk, 'api_transaction': item,
        'type_match': 'exact' if row.type == item.get('type') else 'legacy_deposit_to_broker_interest',
        'before': before, 'after': after}, default=str, allow_nan=False))
    if audit_entries is None:
        record_reconciliations(db, user_id, platform_id, [entry])
    else:
        audit_entries.append(entry)
    row.platform_id, row.source_namespace = platform_id, T212
    db.flush()  # preserve the PK; the following upsert must find this row
    return {'status': 'adopted', 'record_pk': row.pk}


def record_reconciliations(db, user_id, platform_id, entries):
    if not entries:
        return
    raw = json.dumps({'adoptions': entries}, sort_keys=True, allow_nan=False).encode()
    now = dt.datetime.utcnow()
    db.add(IngestionBatch(user_id=user_id, platform_id=platform_id,
        source='t212_identity_reconciliation', filename='Automatic legacy cash transaction reconciliation',
        checksum=hashlib.sha256(raw).hexdigest(), raw_content=raw, parser_version='api-identity-1',
        status='applied', effective_date=None, history_complete=False,
        options={'purpose': 'identity_reconciliation', 'evidence_observed_at': now.isoformat()},
        baseline=hashlib.sha256(json.dumps([e['before'] for e in entries], sort_keys=True).encode()).hexdigest(),
        review={'summary': f"{len(entries)} existing cash transaction(s) matched to their broker account; amounts and dates preserved.",
            'identity_adoption': {'table': 'transactions', 'count': len(entries),
                'records': [{k: v for k, v in entry.items() if k != 'api_transaction'} for entry in entries],
                'evidence': ['Authenticated account API response', 'Exact provider identifier',
                             'Exact amount and UTC timestamp',
                             'Exact type or recognised legacy deposit-to-interest category']}},
        applied_at=now))


def sync_notice(db, user_id, reconciled):
    remaining = db.query(Transaction).filter_by(user_id=user_id, source_namespace='legacy').count()
    parts = []
    if reconciled:
        parts.append(f'Reconciled {reconciled} legacy cash transaction(s) without changing amounts or dates.')
    if remaining:
        parts.append(f'{remaining} legacy cash transaction(s) still need account/source review; retained without duplication.')
    return ' '.join(parts)
