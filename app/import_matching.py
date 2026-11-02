"""Conservative account-scoped matching after an import has committed."""
import csv
import io
from sqlalchemy import text

from app import scoped_identity as identity
from app.models import IngestionBatch, ImportAuthority


def imported_tickers(batch):
    reader = csv.DictReader(io.StringIO(batch.raw_content.decode('utf-8-sig').lstrip('\ufeff')))
    result = set()
    for row in reader:
        clean = {str(k).strip().lower(): str(v or '').strip() for k, v in row.items() if k is not None}
        ticker = next((clean[k] for k in ('ticker', 'symbol', 'slice', 'code', 'epic') if clean.get(k)), None)
        if ticker and ticker.lower() not in {'total', 'totals'}:
            result.add(ticker)
    return sorted(result)


def batch_for(db, user_id, batch_id):
    batch = db.query(IngestionBatch).filter_by(id=batch_id, user_id=user_id).first()
    if batch is None:
        raise LookupError('Import not found')
    if batch.status != 'applied':
        raise ValueError('Apply the import before matching investments')
    from app.ingestion import account_for
    account_for(db, user_id, batch.platform_id)
    return batch


def namespace_for(db, batch):
    from app.ingestion import changes_holdings, scope_for
    if changes_holdings(batch):
        selected = db.get(ImportAuthority, (batch.platform_id, scope_for(batch.source, batch.options)))
        if selected is None or selected.batch_id != batch.id:
            raise ValueError('A newer statement is selected. Match investments from that import instead.')
    return {'ii_holdings': 'holdings:ii_holdings',
            't212_history': 'holdings:t212_history' if batch.history_complete else 'events:t212',
            'ii_transactions': 'events:ii', 'pie_snapshot': 'pie:t212_csv_pie'}.get(batch.source)


def matching_tickers(db, batch):
    namespace = namespace_for(db, batch)
    # Cash movements, footer rows and discarded symbols are not investments.
    present = set(db.execute(text(identity.REFS + '''SELECT ticker FROM scopes
        WHERE platform_id=:p AND user_id=:u AND namespace=:n'''),
        {'p': batch.platform_id, 'u': batch.user_id, 'n': namespace}).scalars())
    return [ticker for ticker in imported_tickers(batch) if ticker in present]


def match_one(db, user_id, batch_id, ticker, lookup=None):
    batch = batch_for(db, user_id, batch_id)
    if ticker not in imported_tickers(batch):
        raise LookupError('Investment not found in this import')
    namespace = namespace_for(db, batch)
    if not namespace:
        return {'ticker': ticker, 'status': 'needs_attention', 'reason': 'Unsupported import format.'}
    from app.automatic_matching import match_scope
    return match_scope(db, user_id, batch.platform_id, namespace, ticker, lookup)
