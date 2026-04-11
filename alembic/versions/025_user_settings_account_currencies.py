"""Add per-account currency codes to user settings

Revision ID: 025
Revises: 024
Create Date: 2026-04-11
"""
from alembic import op

revision = "025"
down_revision = "024"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS trading_currency_code VARCHAR(10)")
    op.execute("ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS isa_currency_code VARCHAR(10)")


def downgrade():
    op.execute("ALTER TABLE user_settings DROP COLUMN IF EXISTS isa_currency_code")
    op.execute("ALTER TABLE user_settings DROP COLUMN IF EXISTS trading_currency_code")
