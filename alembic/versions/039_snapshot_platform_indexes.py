"""Correct snapshot uniqueness after the platform migration.

Revision 033 dropped constraints, but 022/030 created standalone indexes.
Its CREATE INDEX IF NOT EXISTS therefore left account-based indexes intact.
Build replacements first: any duplicate platform keys abort the transaction
without removing old indexes or changing any snapshot rows.
"""
from alembic import op

revision = "039"
down_revision = "038"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE UNIQUE INDEX uq_holding_snapshots_platform_daily
        ON holding_snapshots (user_id, snapshot_date, platform_id, ticker)
    """)
    op.execute("""
        CREATE UNIQUE INDEX uq_portfolio_snapshots_platform_daily
        ON portfolio_snapshots
            (user_id, snapshot_date, platform_id, scope_type, COALESCE(pie_id, -1))
    """)
    # Preserve the previous uniqueness rule for unresolved legacy rows.
    op.execute("""
        CREATE UNIQUE INDEX uq_holding_snapshots_legacy_daily
        ON holding_snapshots (user_id, snapshot_date, account, ticker)
        WHERE platform_id IS NULL
    """)
    op.execute("""
        CREATE UNIQUE INDEX uq_portfolio_snapshots_legacy_daily
        ON portfolio_snapshots
            (user_id, snapshot_date, account, scope_type, COALESCE(pie_id, -1))
        WHERE platform_id IS NULL
    """)
    op.execute("DROP INDEX IF EXISTS uq_holding_snapshots_daily_ticker")
    op.execute("DROP INDEX IF EXISTS uq_portfolio_snapshots_daily_scope")


def downgrade():
    raise RuntimeError(
        "Revision 039 requires application rollback without schema downgrade. "
        "Restoring account-based uniqueness could conflict with preserved platform data."
    )
