"""add auto_sync_enabled to user_settings

Revision ID: 019
Revises: 018
Create Date: 2026-04-07
"""
from alembic import op

revision = "019"
down_revision = "018"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS auto_sync_enabled BOOLEAN NOT NULL DEFAULT TRUE"
    )


def downgrade():
    op.execute("ALTER TABLE user_settings DROP COLUMN IF EXISTS auto_sync_enabled")
