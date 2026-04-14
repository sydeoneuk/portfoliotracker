"""Add pie holding snapshots and dividend payment allocations

Revision ID: 031
Revises: 030
Create Date: 2026-04-14
"""
from alembic import op

revision = "031"
down_revision = "030"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS pie_holding_snapshots (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            pie_id INTEGER NOT NULL REFERENCES pies(pk) ON DELETE CASCADE,
            ticker VARCHAR NOT NULL REFERENCES instruments(ticker),
            account VARCHAR(20),
            snapshot_date DATE NOT NULL,
            captured_at TIMESTAMP NOT NULL,
            owned_quantity DOUBLE PRECISION NOT NULL DEFAULT 0,
            current_share DOUBLE PRECISION,
            expected_share DOUBLE PRECISION
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_pie_holding_snapshots_daily_ticker
        ON pie_holding_snapshots (
            user_id,
            pie_id,
            ticker,
            snapshot_date
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_pie_holding_snapshots_user_pie_date "
        "ON pie_holding_snapshots(user_id, pie_id, snapshot_date)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_pie_holding_snapshots_user_ticker_date "
        "ON pie_holding_snapshots(user_id, ticker, snapshot_date)"
    )

    op.execute("""
        CREATE TABLE IF NOT EXISTS dividend_payment_allocations (
            id SERIAL PRIMARY KEY,
            dividend_payment_id INTEGER NOT NULL REFERENCES dividend_payments(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id),
            pie_id INTEGER NOT NULL REFERENCES pies(pk) ON DELETE CASCADE,
            ticker VARCHAR NOT NULL REFERENCES instruments(ticker),
            account VARCHAR(20) NOT NULL,
            amount_gbp DOUBLE PRECISION NOT NULL DEFAULT 0,
            quantity DOUBLE PRECISION NOT NULL DEFAULT 0,
            allocation_ratio DOUBLE PRECISION NOT NULL DEFAULT 0,
            basis_snapshot_date DATE,
            synced_at TIMESTAMP NOT NULL
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_dividend_payment_allocations_payment_pie
        ON dividend_payment_allocations (
            dividend_payment_id,
            pie_id
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_dividend_payment_allocations_user_pie "
        "ON dividend_payment_allocations(user_id, pie_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_dividend_payment_allocations_user_ticker "
        "ON dividend_payment_allocations(user_id, ticker)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_dividend_payment_allocations_basis_date "
        "ON dividend_payment_allocations(basis_snapshot_date)"
    )


def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_dividend_payment_allocations_basis_date")
    op.execute("DROP INDEX IF EXISTS ix_dividend_payment_allocations_user_ticker")
    op.execute("DROP INDEX IF EXISTS ix_dividend_payment_allocations_user_pie")
    op.execute("DROP INDEX IF EXISTS uq_dividend_payment_allocations_payment_pie")
    op.execute("DROP TABLE IF EXISTS dividend_payment_allocations")
    op.execute("DROP INDEX IF EXISTS ix_pie_holding_snapshots_user_ticker_date")
    op.execute("DROP INDEX IF EXISTS ix_pie_holding_snapshots_user_pie_date")
    op.execute("DROP INDEX IF EXISTS uq_pie_holding_snapshots_daily_ticker")
    op.execute("DROP TABLE IF EXISTS pie_holding_snapshots")
