"""actual dividend payments from T212

Revision ID: 004
Revises: 003
Create Date: 2026-04-05
"""
from alembic import op
import sqlalchemy as sa

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "dividend_payments",
        sa.Column("reference", sa.String(), primary_key=True),
        sa.Column("ticker", sa.String(), sa.ForeignKey("instruments.ticker"), nullable=False),
        sa.Column("account", sa.String(), nullable=False),
        sa.Column("quantity", sa.Float()),
        sa.Column("amount", sa.Float()),
        sa.Column("gross_amount_per_share", sa.Float()),
        sa.Column("paid_on", sa.DateTime(timezone=True)),
        sa.Column("type", sa.String()),
        sa.Column("synced_at", sa.DateTime()),
    )
    op.create_index("ix_dividend_payments_ticker", "dividend_payments", ["ticker"])
    op.create_index("ix_dividend_payments_account", "dividend_payments", ["account"])


def downgrade():
    op.drop_table("dividend_payments")
