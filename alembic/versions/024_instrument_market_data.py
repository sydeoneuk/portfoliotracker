"""Add instrument market data fields

Revision ID: 024
Revises: 023
Create Date: 2026-04-11
"""
from alembic import op

revision = "024"
down_revision = "023"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE instruments ADD COLUMN IF NOT EXISTS shares_outstanding DOUBLE PRECISION")
    op.execute("ALTER TABLE instruments ADD COLUMN IF NOT EXISTS next_ex_dividend_date TIMESTAMP")


def downgrade():
    op.execute("ALTER TABLE instruments DROP COLUMN IF EXISTS next_ex_dividend_date")
    op.execute("ALTER TABLE instruments DROP COLUMN IF EXISTS shares_outstanding")
