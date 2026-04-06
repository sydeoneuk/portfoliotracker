"""Add user_id and account to orders

Revision ID: 012
Revises: 011
Create Date: 2026-04-06
"""
from alembic import op
import sqlalchemy as sa

revision = "012"
down_revision = "011"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("orders", sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True))
    op.add_column("orders", sa.Column("account", sa.String(20), nullable=True))
    op.create_index("ix_orders_user_id", "orders", ["user_id"])


def downgrade():
    op.drop_index("ix_orders_user_id", "orders")
    op.drop_column("orders", "account")
    op.drop_column("orders", "user_id")
