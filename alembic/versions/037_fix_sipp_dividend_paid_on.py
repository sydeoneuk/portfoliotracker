"""Fix NULL paid_on on SIPP CSV-imported dividends

Revision ID: 037
Revises: 036
Create Date: 2026-10-31

T212 SIPP CSV exports timestamps with a UTC offset suffix
(e.g. "2026-10-28 14:18:07+00:00"). _parse_dt did not handle this format
so paid_on was stored as NULL for all SIPP-imported dividend rows.

This migration recovers the correct datetime by extracting the
"YYYY-MM-DD HH24:MI:SS" portion embedded in the synthesised reference
(div_{platform_id}_{ticker}_{timestamp}) and back-filling paid_on.
"""
from alembic import op

revision = '037'
down_revision = '036'
branch_labels = None
depends_on = None


def upgrade():
    op.execute(r"""
        UPDATE dividend_payments
        SET paid_on = SUBSTRING(reference FROM '\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}')::timestamp
        WHERE reference LIKE 'div\_%' ESCAPE '\'
          AND paid_on IS NULL
          AND reference ~ '\d{4}-\d{2}-\d{2}'
    """)


def downgrade():
    pass  # Restoring NULLs is not desirable
