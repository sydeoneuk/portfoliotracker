"""Add instrument fallback price fields

Revision ID: 023
Revises: 022
Create Date: 2026-04-11
"""
from alembic import op

revision = "023"
down_revision = "022"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE instruments ADD COLUMN IF NOT EXISTS fallback_price DOUBLE PRECISION")
    op.execute("ALTER TABLE instruments ADD COLUMN IF NOT EXISTS fallback_price_source VARCHAR(20)")
    op.execute("ALTER TABLE instruments ADD COLUMN IF NOT EXISTS fallback_price_updated_at TIMESTAMP")


def downgrade():
    op.execute("ALTER TABLE instruments DROP COLUMN IF EXISTS fallback_price_updated_at")
    op.execute("ALTER TABLE instruments DROP COLUMN IF EXISTS fallback_price_source")
    op.execute("ALTER TABLE instruments DROP COLUMN IF EXISTS fallback_price")
