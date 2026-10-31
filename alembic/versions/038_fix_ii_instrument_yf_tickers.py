"""Fix yf_ticker for ii-imported GBP/GBX instruments missing the .L suffix

Revision ID: 038
Revises: 037
Create Date: 2026-10-31

When the ii CSV importer created or updated instrument records it did not
set yf_ticker.  yfinance enrichment then looked up the bare ticker (e.g.
'FWRG') and may have resolved it to a US stock instead of the correct
LSE-listed instrument ('FWRG.L').

This migration sets yf_ticker to '<ticker>.L' for every GBP/GBX instrument
that has at least one position on an Interactive Investor platform AND whose
yf_ticker is currently NULL, empty, or equal to the bare ticker (i.e. no
exchange suffix has been applied yet or the wrong value was stored).

Instruments where yf_ticker already has a suffix (e.g. '.L', '.DE') or was
manually corrected to something other than the bare ticker are left untouched.
"""
from alembic import op

revision = '038'
down_revision = '037'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        UPDATE instruments i
        SET yf_ticker = i.ticker || '.L'
        WHERE i.currency_code IN ('GBP', 'GBX')
          AND (
              i.yf_ticker IS NULL
              OR i.yf_ticker = ''
              OR i.yf_ticker = i.ticker
          )
          AND EXISTS (
              SELECT 1
              FROM positions p
              JOIN platforms pl ON pl.id = p.platform_id
              WHERE p.ticker = i.ticker
                AND pl.platform_type = 'interactive_investor'
          )
    """)


def downgrade():
    # Restore bare ticker where we added .L (only for instruments still matching
    # the upgrade criteria, i.e. we added .L and nothing else changed it since)
    op.execute("""
        UPDATE instruments i
        SET yf_ticker = i.ticker
        WHERE i.yf_ticker = i.ticker || '.L'
          AND i.currency_code IN ('GBP', 'GBX')
          AND EXISTS (
              SELECT 1
              FROM positions p
              JOIN platforms pl ON pl.id = p.platform_id
              WHERE p.ticker = i.ticker
                AND pl.platform_type = 'interactive_investor'
          )
    """)
