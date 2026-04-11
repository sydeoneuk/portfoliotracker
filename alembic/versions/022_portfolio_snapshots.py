"""Add daily portfolio snapshots

Revision ID: 022
Revises: 021
Create Date: 2026-04-11
"""
from alembic import op

revision = "022"
down_revision = "021"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS portfolio_snapshots (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            snapshot_date DATE NOT NULL,
            captured_at TIMESTAMP NOT NULL,
            account VARCHAR(20) NOT NULL,
            scope_type VARCHAR(20) NOT NULL,
            pie_id INTEGER NULL REFERENCES pies(pk) ON DELETE CASCADE,
            total_value_gbp DOUBLE PRECISION NOT NULL DEFAULT 0
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_portfolio_snapshots_daily_scope
        ON portfolio_snapshots (
            user_id,
            snapshot_date,
            account,
            scope_type,
            COALESCE(pie_id, -1)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_portfolio_snapshots_user_date ON portfolio_snapshots(user_id, snapshot_date)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_portfolio_snapshots_pie ON portfolio_snapshots(pie_id)")


def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_portfolio_snapshots_pie")
    op.execute("DROP INDEX IF EXISTS ix_portfolio_snapshots_user_date")
    op.execute("DROP INDEX IF EXISTS uq_portfolio_snapshots_daily_scope")
    op.execute("DROP TABLE IF EXISTS portfolio_snapshots")
