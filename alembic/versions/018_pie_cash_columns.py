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
    op.add_column("user_settings", sa.Column("pie_cash_trading", sa.Float(), nullable=True))
    op.add_column("user_settings", sa.Column("pie_cash_isa", sa.Float(), nullable=True))


def downgrade():
    op.drop_column("user_settings", "pie_cash_trading")
    op.drop_column("user_settings", "pie_cash_isa")
