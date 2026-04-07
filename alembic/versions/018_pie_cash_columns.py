"""Add pie_cash columns to user_settings

Stores uninvested cash sitting inside pies per account, sourced from the
account cash endpoint (cash.inPies). Separate from free_cash_* which tracks
cash outside pies.

Revision ID: 018
Revises: 017
Create Date: 2026-04-07
"""
from alembic import op
import sqlalchemy as sa

revision = "018"
down_revision = "017"
branch_labels = None
depends_on = None


def upgrade():
    # Use IF NOT EXISTS because migration 017 was amended after initial rollout
    # and may already contain these columns on databases created from the updated 017.
    op.execute("ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS pie_cash_trading FLOAT")
    op.execute("ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS pie_cash_isa FLOAT")


def downgrade():
    op.drop_column("user_settings", "pie_cash_trading")
    op.drop_column("user_settings", "pie_cash_isa")
