"""Add platform_id to synthesised dividend references

Revision ID: 036
Revises: 035
Create Date: 2026-10-31

Previously the T212 CSV importer synthesised dividend references as
`div_{ticker}_{time}`. Because the unique constraint on dividend_payments
is (user_id, reference), importing dividends from two accounts (e.g. ISA
and SIPP) for the same ticker on the same day produced the same reference
and the second import silently overwrote the first.

The new format is `div_{platform_id}_{ticker}_{time}`, making each
platform's dividends independently tracked.

This migration updates all existing synthesised references in place so
that re-imports after the code change produce matching references and
upserts continue to work correctly. Records without a platform_id are
left untouched since their reference format cannot be reliably updated.
"""
from alembic import op

revision = '036'
down_revision = '035'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        UPDATE dividend_payments
        SET reference = 'div_' || platform_id || '_' || SUBSTR(reference, 5)
        WHERE reference LIKE 'div\\_%' ESCAPE '\\'
          AND platform_id IS NOT NULL
    """)


def downgrade():
    # Reverse: strip `{platform_id}_` from references that were updated above.
    # Matches the pattern 'div_{digits}_{rest}' and collapses to 'div_{rest}'.
    op.execute("""
        UPDATE dividend_payments
        SET reference = 'div_' || REGEXP_REPLACE(SUBSTR(reference, 5), '^[0-9]+_', '')
        WHERE reference ~ '^div_[0-9]+_'
          AND platform_id IS NOT NULL
    """)
