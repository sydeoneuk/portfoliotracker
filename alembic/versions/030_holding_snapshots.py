"""Add daily holding snapshots

Revision ID: 030
Revises: 029
Create Date: 2026-04-13
"""
from alembic import op

revision = "030"
down_revision = "029"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS holding_snapshots (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            snapshot_date DATE NOT NULL,
            captured_at TIMESTAMP NOT NULL,
            account VARCHAR(20) NOT NULL,
            ticker VARCHAR NOT NULL REFERENCES instruments(ticker),
            quantity DOUBLE PRECISION NOT NULL DEFAULT 0,
            price_native DOUBLE PRECISION NOT NULL DEFAULT 0,
            value_gbp DOUBLE PRECISION NOT NULL DEFAULT 0
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_holding_snapshots_daily_ticker
        ON holding_snapshots (
            user_id,
            snapshot_date,
            account,
            ticker
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_holding_snapshots_user_ticker_date "
        "ON holding_snapshots(user_id, ticker, snapshot_date)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_holding_snapshots_account_date "
        "ON holding_snapshots(account, snapshot_date)"
    )


def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_holding_snapshots_account_date")
    op.execute("DROP INDEX IF EXISTS ix_holding_snapshots_user_ticker_date")
    op.execute("DROP INDEX IF EXISTS uq_holding_snapshots_daily_ticker")
    op.execute("DROP TABLE IF EXISTS holding_snapshots")
