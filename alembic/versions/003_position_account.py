"""add account column to positions

Revision ID: 003
Revises: 002
Create Date: 2026-04-05
"""
from alembic import op
import sqlalchemy as sa

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade():
    # 1. Add account column, defaulting existing rows to "Trading"
    op.add_column("positions", sa.Column("account", sa.String(), nullable=True))
    op.execute("UPDATE positions SET account = 'Trading'")
    op.alter_column("positions", "account", nullable=False)

    # 2. Drop old single-column primary key, create composite PK
    op.drop_constraint("positions_pkey", "positions", type_="primary")
    op.create_primary_key("positions_pkey", "positions", ["ticker", "account"])


def downgrade():
    op.drop_constraint("positions_pkey", "positions", type_="primary")
    op.create_primary_key("positions_pkey", "positions", ["ticker"])
    op.drop_column("positions", "account")
