"""Add cash columns to pies and user_settings

pies.cash           — uninvested cash sitting in the pie (from T212 pie detail result.cash)
user_settings.free_cash_trading — uninvested cash in Trading account outside pies
user_settings.free_cash_isa     — uninvested cash in ISA account outside pies

Revision ID: 017
Revises: 016
Create Date: 2026-04-07
"""
from alembic import op
import sqlalchemy as sa

revision = "017"
down_revision = "016"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("pies", sa.Column("cash", sa.Float(), nullable=True))
    op.add_column("user_settings", sa.Column("free_cash_trading", sa.Float(), nullable=True))
    op.add_column("user_settings", sa.Column("free_cash_isa", sa.Float(), nullable=True))
    op.add_column("user_settings", sa.Column("pie_cash_trading", sa.Float(), nullable=True))
    op.add_column("user_settings", sa.Column("pie_cash_isa", sa.Float(), nullable=True))


def downgrade():
    op.drop_column("pies", "cash")
    op.drop_column("user_settings", "free_cash_trading")
    op.drop_column("user_settings", "free_cash_isa")
    op.drop_column("user_settings", "pie_cash_trading")
    op.drop_column("user_settings", "pie_cash_isa")
