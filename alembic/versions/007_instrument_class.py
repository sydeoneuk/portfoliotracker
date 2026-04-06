"""add instrument_class override column

Revision ID: 007
Revises: 006
Create Date: 2026-04-05
"""
from alembic import op
import sqlalchemy as sa

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("instruments", sa.Column("instrument_class", sa.String(50), nullable=True))


def downgrade():
    op.drop_column("instruments", "instrument_class")
