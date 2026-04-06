"""add api secret columns to user_settings

Revision ID: 010
Revises: 009
Create Date: 2026-04-05
"""
from alembic import op
import sqlalchemy as sa

revision = "010"
down_revision = "009"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("user_settings", sa.Column("t212_api_secret_enc", sa.Text()))
    op.add_column("user_settings", sa.Column("t212_isa_api_secret_enc", sa.Text()))


def downgrade():
    op.drop_column("user_settings", "t212_isa_api_secret_enc")
    op.drop_column("user_settings", "t212_api_secret_enc")
