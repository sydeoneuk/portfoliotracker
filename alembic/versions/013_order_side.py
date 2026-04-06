"""Add side column to orders

Revision ID: 013
Revises: 012
Create Date: 2026-04-06
"""
from alembic import op
import sqlalchemy as sa

revision = "013"
down_revision = "012"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("orders", sa.Column("side", sa.String(10), nullable=True))


def downgrade():
    op.drop_column("orders", "side")
