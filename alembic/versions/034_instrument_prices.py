"""Add instrument_prices table for multi-provider price storage

Revision ID: 034
Revises: 033
Create Date: 2026-10-30

Replaces the ad-hoc instruments.fallback_price column and positions.current_price
with a dedicated price store that supports multiple providers (t212_api, yfinance,
manual). price is in the instrument's native currency; price_gbp is the GBP
equivalent computed at fetch time.
"""
from alembic import op

revision = '034'
down_revision = '033'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE instrument_prices (
            ticker      VARCHAR     NOT NULL REFERENCES instruments(ticker) ON DELETE CASCADE,
            source      VARCHAR(50) NOT NULL,
            price       NUMERIC(18,6) NOT NULL,
            price_gbp   NUMERIC(18,6),
            currency    VARCHAR(10) NOT NULL DEFAULT 'GBP',
            updated_at  TIMESTAMP   NOT NULL DEFAULT NOW(),
            PRIMARY KEY (ticker, source)
        )
    """)
    op.execute(
        "CREATE INDEX ix_instrument_prices_ticker ON instrument_prices(ticker)"
    )


def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_instrument_prices_ticker")
    op.execute("DROP TABLE IF EXISTS instrument_prices")
