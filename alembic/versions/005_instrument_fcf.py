"""add fcf_per_share_3y_avg to instruments

Revision ID: 005
Revises: 004
Create Date: 2026-04-05
"""
from alembic import op
import sqlalchemy as sa

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("instruments", sa.Column("fcf_per_share_3y_avg", sa.Float(), nullable=True))


def downgrade():
    op.drop_column("instruments", "fcf_per_share_3y_avg")
