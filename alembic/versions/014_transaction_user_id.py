"""Add user_id to transactions

Revision ID: 014
Revises: 013
Create Date: 2026-04-06
"""
from alembic import op
import sqlalchemy as sa

revision = "014"
down_revision = "013"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("transactions", sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True))
    op.create_index("ix_transactions_user_id", "transactions", ["user_id"])


def downgrade():
    op.drop_index("ix_transactions_user_id", "transactions")
    op.drop_column("transactions", "user_id")
