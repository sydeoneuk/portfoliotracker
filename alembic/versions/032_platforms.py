"""Add platforms table and platform_id FK columns

Revision ID: 032
Revises: 031
Create Date: 2026-10-30
"""
from alembic import op

revision = "032"
down_revision = "031"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS platforms (
            id          SERIAL PRIMARY KEY,
            user_id     INTEGER NOT NULL REFERENCES users(id),
            name        VARCHAR(100) NOT NULL,
            platform_type VARCHAR(50) NOT NULL,
            account_type  VARCHAR(20) NOT NULL,
            currency    VARCHAR(10) NOT NULL DEFAULT 'GBP',
            is_active   BOOLEAN NOT NULL DEFAULT true,
            created_at  TIMESTAMP NOT NULL DEFAULT NOW(),
            updated_at  TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_platforms_user_id ON platforms(user_id)"
    )

    # Add nullable platform_id FK to every table that currently uses account string
    tables = [
        "positions",
        "orders",
        "transactions",
        "dividend_payments",
        "portfolio_snapshots",
        "holding_snapshots",
        "pies",
        "pie_holding_snapshots",
        "dividend_payment_allocations",
    ]
    for table in tables:
        op.execute(
            f"ALTER TABLE {table} "
            f"ADD COLUMN IF NOT EXISTS platform_id INTEGER REFERENCES platforms(id)"
        )
        op.execute(
            f"CREATE INDEX IF NOT EXISTS ix_{table}_platform_id "
            f"ON {table}(platform_id)"
        )


def downgrade():
    tables = [
        "dividend_payment_allocations",
        "pie_holding_snapshots",
        "pies",
        "holding_snapshots",
        "portfolio_snapshots",
        "dividend_payments",
        "transactions",
        "orders",
        "positions",
    ]
    for table in tables:
        op.execute(f"DROP INDEX IF EXISTS ix_{table}_platform_id")
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS platform_id")

    op.execute("DROP TABLE IF EXISTS platforms")
