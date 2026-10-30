"""Populate instrument_prices from existing positions and instruments data

Revision ID: 035
Revises: 034
Create Date: 2026-10-30

Seeds instrument_prices from two sources:
  1. instruments.fallback_price  (yfinance / manual prices already on file)
  2. positions.current_price     (most-recent T212 API price per ticker)

price_gbp is set equal to price for GBP instruments; left NULL for non-GBP
instruments (yfinance will populate it correctly on first refresh).

This migration is fully data-safe — it only inserts/updates instrument_prices
and does not touch existing columns on positions or instruments.
"""
from alembic import op

revision = '035'
down_revision = '034'
branch_labels = None
depends_on = None


def upgrade():
    # 1. Seed from instruments.fallback_price
    op.execute("""
        INSERT INTO instrument_prices (ticker, source, price, price_gbp, currency, updated_at)
        SELECT
            i.ticker,
            CASE
                WHEN i.fallback_price_source IS NOT NULL
                     AND i.fallback_price_source <> ''
                THEN i.fallback_price_source
                ELSE 'manual'
            END                                           AS source,
            i.fallback_price                              AS price,
            CASE WHEN COALESCE(i.currency_code, 'GBP') = 'GBP'
                 THEN i.fallback_price ELSE NULL END      AS price_gbp,
            COALESCE(i.currency_code, 'GBP')             AS currency,
            COALESCE(i.fallback_price_updated_at, NOW()) AS updated_at
        FROM instruments i
        WHERE i.fallback_price IS NOT NULL
        ON CONFLICT (ticker, source) DO UPDATE SET
            price      = EXCLUDED.price,
            price_gbp  = EXCLUDED.price_gbp,
            updated_at = EXCLUDED.updated_at
        WHERE EXCLUDED.updated_at > instrument_prices.updated_at
    """)

    # 2. Seed from positions.current_price — most-recent API-synced price per ticker
    op.execute("""
        INSERT INTO instrument_prices (ticker, source, price, price_gbp, currency, updated_at)
        SELECT DISTINCT ON (p.ticker)
            p.ticker,
            't212_api'                                                AS source,
            p.current_price                                           AS price,
            CASE WHEN COALESCE(i.currency_code, 'GBP') = 'GBP'
                 THEN p.current_price ELSE NULL END                   AS price_gbp,
            COALESCE(i.currency_code, 'GBP')                         AS currency,
            COALESCE(p.last_synced_at, NOW())                         AS updated_at
        FROM positions p
        JOIN instruments i ON i.ticker = p.ticker
        WHERE p.current_price IS NOT NULL
        ORDER BY p.ticker, p.last_synced_at DESC NULLS LAST
        ON CONFLICT (ticker, source) DO UPDATE SET
            price      = EXCLUDED.price,
            price_gbp  = EXCLUDED.price_gbp,
            updated_at = EXCLUDED.updated_at
        WHERE EXCLUDED.updated_at > instrument_prices.updated_at
    """)


def downgrade():
    op.execute("DELETE FROM instrument_prices")
