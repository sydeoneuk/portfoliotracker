"""add account column to pies

Revision ID: 011
Revises: 010
Create Date: 2026-04-06
"""
from alembic import op
import sqlalchemy as sa

revision = "011"
down_revision = "010"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("pies", sa.Column("account", sa.String(20)))


def downgrade():
    op.drop_column("pies", "account")
