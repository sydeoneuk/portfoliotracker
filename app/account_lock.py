"""A lock held across commits, shared by API sync and staged CSV application."""
from contextlib import contextmanager
from sqlalchemy import text


@contextmanager
def account_write_lock(db, platform_id):
    if platform_id is None:
        raise ValueError('An explicit account is required for writing')
    engine = db.get_bind().engine
    with engine.connect().execution_options(isolation_level='AUTOCOMMIT') as connection:
        acquired = connection.execute(text('SELECT pg_try_advisory_lock(:namespace, :account)'),
            {'namespace': 5526066, 'account': platform_id}).scalar()
        if not acquired:
            raise ValueError('This account is being updated. Retry after its import or sync completes.')
        try:
            yield
        finally:
            connection.execute(text('SELECT pg_advisory_unlock(:namespace, :account)'),
                {'namespace': 5526066, 'account': platform_id})
