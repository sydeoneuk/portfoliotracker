"""add eps_ttm to instruments

Revision ID: 006
Revises: 005
Create Date: 2026-04-05
"""
from alembic import op
import sqlalchemy as sa

revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("instruments", sa.Column("eps_ttm", sa.Float(), nullable=True))


def downgrade():
    op.drop_column("instruments", "eps_ttm")
