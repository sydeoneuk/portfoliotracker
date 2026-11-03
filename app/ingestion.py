"""Stage, review and atomically apply source imports without losing evidence."""
import csv
import datetime as dt
import hashlib
import io
import json
import math
from dataclasses import asdict
from sqlalchemy import text
from app.account_lock import account_write_lock
from app.import_preview import import_effect
from app.models import Platform, Position, IngestionBatch, IngestionRow, ImportHoldingObservation, ImportAuthority

PARSER_VERSION = 'csv-2'
SOURCES = {'t212_history', 'ii_holdings', 'ii_transactions', 'pie_snapshot', 'purchase_costs'}
MAX_UPLOAD = 10 * 1024 * 1024


def json_value(value):
    if isinstance(value, dict):
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_value(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)  # preserve legacy nonfinite observations without invalid JSON
    return value


def account_for(db, user_id, platform_id):
    account = db.query(Platform).filter_by(id=platform_id, user_id=user_id, is_active=True).first()
    if account is None:
        raise LookupError('Account not found')
    return account


def scope_for(source, options):
    return 'pie:' + options['pie_name'] if source == 'pie_snapshot' else 'account'


def changes_holdings(batch):
    return batch.source in {'ii_holdings', 'pie_snapshot'} or (batch.source == 't212_history' and batch.history_complete)


def state(db, user_id, platform_id):
    result = {}
    for table in ('positions', 'orders', 'transactions', 'dividend_payments', 'pies'):
        result[table] = db.execute(text(f'SELECT row_to_json(t)::text FROM {table} t WHERE user_id=:u AND platform_id=:p'),
            {'u': user_id, 'p': platform_id}).scalars().all()
        result[table].sort()
    result['pie_holdings'] = sorted(db.execute(text('''SELECT row_to_json(h)::text FROM pie_holdings h
        JOIN pies p ON p.pk=h.pie_id WHERE p.user_id=:u AND p.platform_id=:p'''),
        {'u': user_id, 'p': platform_id}).scalars().all())
    result['instrument_identity'] = sorted(db.execute(text('''SELECT json_build_array(i.ticker,i.isin,i.currency_code,i.mic_code,i.yf_ticker)::text
        FROM instruments i WHERE i.ticker IN (SELECT ticker FROM positions WHERE user_id=:u AND platform_id=:p
        UNION SELECT ticker FROM orders WHERE user_id=:u AND platform_id=:p)'''), {'u': user_id, 'p': platform_id}).scalars().all())
    return hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()


def holdings(db, batch, *, full=False):
    if batch.source == 'pie_snapshot':
        rows = db.execute(text('''SELECT h.* FROM pie_holdings h JOIN pies p ON p.pk=h.pie_id
            WHERE p.user_id=:u AND p.platform_id=:p AND p.name=:name AND p.source_namespace='t212_csv_pie' ORDER BY h.ticker'''),
            {'u': batch.user_id, 'p': batch.platform_id, 'name': batch.options['pie_name']}).mappings().all()
        rows = [{**dict(row), 'quantity': row['owned_quantity']} for row in rows]
        if not full:
            rows = [{key: value for key, value in row.items() if key not in {'id', 'pie_id', 'synced_at'}} for row in rows]
    else:
        # Adapters use SQL upserts. Reload cached ORM objects so a preview cannot
        # show the previous statement's costs or quantities after a dry run.
        rows = db.query(Position).filter_by(user_id=batch.user_id, platform_id=batch.platform_id).populate_existing().order_by(Position.ticker).all()
        rows = [{column.name: getattr(row, column.name) for column in Position.__table__.columns
                 if full or column.name != 'last_synced_at'} for row in rows]
    return json.loads(json.dumps(json_value([dict(row) for row in rows]), default=str, allow_nan=False))


def run_adapter(db, batch):
    platform = account_for(db, batch.user_id, batch.platform_id)
    args = dict(session=db, platform=platform, user_id=batch.user_id, managed=True)
    if batch.source == 'purchase_costs':
        from app.importers.purchase_cost_csv import import_purchase_costs
        result = import_purchase_costs(db, platform, batch)
        db.flush()
        return result
    if batch.source == 'ii_holdings':
        from app.importers.ii_csv import IiCsvImporter
        importer = IiCsvImporter(**args)
    elif batch.source == 't212_history':
        from app.importers.t212_csv import T212CsvImporter
        importer = T212CsvImporter(**args, rebuild_positions=batch.history_complete, replace_observations=batch.history_complete)
    elif batch.source == 'ii_transactions':
        from app.importers.ii_transactions_csv import IiTransactionsCsvImporter
        importer = IiTransactionsCsvImporter(**args)
    else:
        from app.importers.t212_pie_csv import T212PieCsvImporter
        importer = T212PieCsvImporter(**args, pie_name=batch.options['pie_name'])
    result = importer.import_file(io.BytesIO(batch.raw_content))
    if result.errors:
        raise ValueError('; '.join(result.errors))
    db.flush()
    return result


def validate_authority(db, batch):
    if batch.source == 'purchase_costs':
        selected = db.get(ImportAuthority, (batch.platform_id, 'account'))
        if selected and selected.effective_date > batch.effective_date and not batch.options.get('confirm_unchanged_cost'):
            raise ValueError('This cost statement is older than the selected holdings statement. Confirm the supplied costs still apply to the current holdings.')
        return
    if not changes_holdings(batch):
        return
    account = account_for(db, batch.user_id, batch.platform_id)
    if account.platform_type == 't212_api' and batch.source != 'pie_snapshot':
        raise ValueError('API accounts retain broker holdings authority. Import transaction events without selecting complete-history holdings.')
    selected = db.get(ImportAuthority, (batch.platform_id, scope_for(batch.source, batch.options)))
    if selected and selected.effective_date > batch.effective_date:
        raise ValueError(f'Older observation ({batch.effective_date}) cannot replace selected holdings dated {selected.effective_date}.')
    if selected and selected.effective_date == batch.effective_date and selected.batch_id != batch.id:
        previous = db.get(IngestionBatch, selected.batch_id)
        if previous.checksum != batch.checksum:
            raise ValueError('A different observation is already selected for this date. Conflicting same-day snapshots require reconciliation.')
    if batch.history_complete:
        from app.importers.t212_csv import _normalise_headers, _get, _parse_dt
        reader = csv.DictReader(io.StringIO(batch.raw_content.decode('utf-8-sig')))
        headers = _normalise_headers(reader.fieldnames or [])
        start, end = dt.date.fromisoformat(batch.options['coverage_start']), dt.date.fromisoformat(batch.options['coverage_end'])
        for row in reader:
            observed = _parse_dt(_get(row, headers, 'time'))
            if observed is None or not start <= observed.date() <= end:
                raise ValueError('A source row has no valid date within the declared complete-history coverage.')
        outside = db.execute(text('''SELECT COUNT(*) FROM orders WHERE user_id=:u AND platform_id=:p
            AND (COALESCE(filled_at,created_at) IS NULL OR COALESCE(filled_at,created_at)::date < :start
            OR COALESCE(filled_at,created_at)::date > :end)'''), {'u': batch.user_id, 'p': batch.platform_id, 'start': start, 'end': end}).scalar()
        if outside:
            raise ValueError('Existing trades fall outside the declared complete history. Preserve current holdings and reconcile coverage first.')


def stage(db, user_id, platform_id, source, content, filename, *, effective_date=None, history_complete=False,
          coverage_start=None, coverage_end=None, pie_name=None, confirm_unchanged_cost=False, replace_purchase_costs=False):
    account_for(db, user_id, platform_id)
    if source not in SOURCES:
        raise ValueError('Unsupported import source')
    if not content or len(content) > MAX_UPLOAD:
        raise ValueError('Upload a nonempty CSV no larger than 10 MB')
    options = {}
    if source == 'purchase_costs' and confirm_unchanged_cost:
        options['confirm_unchanged_cost'] = True
    if source == 'purchase_costs' and replace_purchase_costs:
        options['replace_purchase_costs'] = True
    def date(value):
        return dt.date.fromisoformat(value) if value else None
    effective = date(effective_date)
    if source in {'ii_holdings', 'pie_snapshot', 'purchase_costs'} and effective is None:
        raise ValueError('Enter the snapshot effective date from your statement')
    if effective and effective > dt.datetime.now(dt.timezone.utc).date():
        raise ValueError('A snapshot cannot be effective in the future')
    if source == 'pie_snapshot':
        if not pie_name or not pie_name.strip() or len(pie_name.strip()) > 160:
            raise ValueError('Enter a pie name of at most 160 characters')
        options['pie_name'] = pie_name.strip()
    if history_complete:
        if source != 't212_history':
            raise ValueError('Complete-history authority is only supported for T212 trade history')
        start, end = date(coverage_start), date(coverage_end)
        if not start or not end or start > end or end > dt.datetime.now(dt.timezone.utc).date():
            raise ValueError('Enter valid complete-history coverage dates, including opening holdings')
        effective = end
        options.update(coverage_start=start.isoformat(), coverage_end=end.isoformat())
    batch = IngestionBatch(user_id=user_id, platform_id=platform_id, source=source,
        filename=filename.replace('\\', '/').rsplit('/', 1)[-1][:255], raw_content=content,
        checksum=hashlib.sha256(content).hexdigest(), parser_version=PARSER_VERSION,
        effective_date=effective, history_complete=history_complete, options=options, status='review', review={})
    with account_write_lock(db, platform_id):
        db.flush()
        batch.baseline = state(db, user_id, platform_id)
        before = holdings(db, batch)
        original_records = holdings(db, batch, full=True)
        selected = db.get(ImportAuthority, (platform_id, scope_for(source, options)))
        authority_info = {'source': 'legacy/unknown', 'effective_date': None}
        if selected:
            selected_batch = db.get(IngestionBatch, selected.batch_id)
            authority_info = {'source': selected_batch.source, 'effective_date': selected.effective_date.isoformat(), 'batch_id': selected.batch_id}
        repeated = db.query(IngestionBatch).filter_by(user_id=user_id, platform_id=platform_id,
            source=source, checksum=batch.checksum, status='applied').order_by(IngestionBatch.id.desc()).first()
        try:
            rows = list(csv.reader(io.StringIO(content.decode('utf-8-sig')), strict=True))
            validate_authority(db, batch)
            transaction = db.begin_nested()
            try:
                result = run_adapter(db, batch)
                proposed = holdings(db, batch)
                proposed_records = holdings(db, batch, full=True)
                review = asdict(result)
                review.update(summary=result.summary(), before=before, proposed=proposed, current_authority=authority_info)
                if repeated:
                    review['exact_file_seen_in_batch'] = repeated.id
            finally:
                transaction.rollback()
            batch.review = json.loads(json.dumps(review, default=str))
        except Exception as exc:
            batch.status = 'rejected'
            batch.review = {'errors': [str(exc)], 'summary': 'Upload retained; no current data changed.'}
            proposed = []
            proposed_records = []
            rows = locals().get('rows', [])
        db.add(batch)
        db.flush()
        for number, cells in enumerate(rows, start=1):
            db.add(IngestionRow(batch_id=batch.id, user_id=user_id, row_number=number, cells=cells))
        for phase, values in (('before', original_records), ('proposed', proposed_records)):
            db.add(ImportHoldingObservation(batch_id=batch.id, user_id=user_id, phase=phase,
                scope=scope_for(source, options), effective_date=effective if phase == 'proposed' else None,
                complete=batch.status != 'rejected' and changes_holdings(batch) and source != 'pie_snapshot' and phase == 'proposed', holdings=values))
        db.commit()
    return batch


def apply(db, user_id, batch_id):
    batch = db.query(IngestionBatch).filter_by(id=batch_id, user_id=user_id).first()
    if batch is None:
        raise LookupError('Import not found')
    with account_write_lock(db, batch.platform_id):
        db.refresh(batch, with_for_update=True)
        if batch.status == 'applied':
            return batch  # safe retry of a completed request
        if batch.status != 'review' or batch.parser_version != PARSER_VERSION:
            raise ValueError('This import cannot be applied. Upload again to obtain a fresh preview.')
        if batch.baseline != state(db, user_id, batch.platform_id):
            raise ValueError('Account changed since preview. Upload again to review current differences.')
        validate_authority(db, batch)
        transaction = db.begin_nested()
        try:
            run_adapter(db, batch)
            if holdings(db, batch) != batch.review['proposed']:
                raise ValueError('Import result changed since preview. No data applied; obtain a fresh preview.')
            if changes_holdings(batch):
                authority = db.get(ImportAuthority, (batch.platform_id, scope_for(batch.source, batch.options)))
                if authority is None:
                    authority = ImportAuthority(platform_id=batch.platform_id, user_id=user_id,
                        scope=scope_for(batch.source, batch.options))
                    db.add(authority)
                authority.batch_id, authority.effective_date = batch.id, batch.effective_date
            db.add(ImportHoldingObservation(batch_id=batch.id, user_id=user_id, phase='applied',
                scope=scope_for(batch.source, batch.options), effective_date=batch.effective_date,
                complete=changes_holdings(batch) and batch.source != 'pie_snapshot', holdings=holdings(db, batch, full=True)))
            batch.status, batch.applied_at = 'applied', dt.datetime.utcnow()
            transaction.commit()
            db.commit()
        except Exception:
            if transaction.is_active:
                transaction.rollback()
            db.rollback()
            raise
    return batch


def describe(batch):
    return {**batch.review, 'batch_id': batch.id, 'status': batch.status, 'source': batch.source,
        'import_effect': import_effect(batch),
        'effective_date': batch.effective_date.isoformat() if batch.effective_date else None,
        'history_complete': batch.history_complete, 'checksum': batch.checksum,
        'parser_version': batch.parser_version, 'filename': batch.filename,
        'warnings': [*batch.review.get('warnings', []), *(['Parser changed since this preview. Upload the file again to review it with the current parser.']
                     if batch.status == 'review' and batch.parser_version != PARSER_VERSION else [])],
        'can_apply': batch.status == 'review' and batch.parser_version == PARSER_VERSION,
        'summary': ('Preview only — ' if batch.status == 'review' else '') + batch.review.get('summary', '')}



def remove_import_evidence(db, user_id, platform_id=None):
    """Only for an explicitly requested account deletion, never normal imports."""
    from sqlalchemy import delete
    from app.models import AccountListingResolution, AccountListingAudit
    for model in (AccountListingAudit, AccountListingResolution):
        scope = delete(model).where(model.user_id == user_id)
        if platform_id is not None:
            scope = scope.where(model.platform_id == platform_id)
        db.execute(scope)
    selected = db.query(IngestionBatch.id).filter_by(user_id=user_id)
    if platform_id is not None:
        selected = selected.filter_by(platform_id=platform_id)
    ids = selected.statement
    db.execute(delete(ImportAuthority).where(ImportAuthority.user_id == user_id, ImportAuthority.batch_id.in_(ids)))
    for model in (IngestionRow, ImportHoldingObservation):
        db.execute(delete(model).where(model.user_id == user_id, model.batch_id.in_(ids)))
    db.execute(delete(IngestionBatch).where(IngestionBatch.id.in_(ids)))


def record_api_observation(db, user_id, platform_id, raw_positions, before, observed_at):
    """Persist broker observations in the same transaction as its projection."""
    content = json.dumps(raw_positions, sort_keys=True, default=str).encode()
    batch = IngestionBatch(user_id=user_id, platform_id=platform_id, source='t212_api', filename='API positions',
        raw_content=content, checksum=hashlib.sha256(content).hexdigest(), parser_version='api-1',
        effective_date=observed_at.date(), history_complete=False,
        options={'date_basis': 'API fetched-at date; broker effective timestamp unavailable'},
        status='applied', applied_at=observed_at, review={'summary': 'Broker holdings observation selected.'})
    db.add(batch)
    db.flush()
    for phase, values in (('before', before), ('applied', holdings(db, batch, full=True))):
        db.add(ImportHoldingObservation(batch_id=batch.id, user_id=user_id, phase=phase, scope='account',
            effective_date=observed_at.date() if phase == 'applied' else None,
            complete=phase == 'applied', holdings=values))
    authority = db.get(ImportAuthority, (platform_id, 'account'))
    if authority is None:
        authority = ImportAuthority(platform_id=platform_id, user_id=user_id, scope='account')
        db.add(authority)
    authority.batch_id, authority.effective_date = batch.id, observed_at.date()
