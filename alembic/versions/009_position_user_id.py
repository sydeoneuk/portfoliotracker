"""add user_id to positions and dividend_payments

Revision ID: 009
Revises: 008
Create Date: 2026-04-05

NOTE: Existing position/dividend_payment data will have user_id = NULL and will
not be visible to any authenticated user. Users must re-sync after this migration.
"""
from alembic import op
import sqlalchemy as sa

revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None


def upgrade():
    # Clear existing data — no user_id, cannot satisfy composite PK; users re-sync after this
    op.execute("TRUNCATE TABLE dividend_payments")
    op.execute("TRUNCATE TABLE positions")

    # positions: drop old PK, add user_id, create new composite PK
    op.add_column("positions", sa.Column("user_id", sa.Integer(),
                  sa.ForeignKey("users.id"), nullable=True))
    op.drop_constraint("positions_pkey", "positions", type_="primary")
    op.execute("""
        ALTER TABLE positions
        ADD CONSTRAINT positions_pkey PRIMARY KEY (ticker, account, user_id)
    """)
    op.create_index("ix_positions_user_id", "positions", ["user_id"])

    # dividend_payments: add user_id column + index
    op.add_column("dividend_payments", sa.Column("user_id", sa.Integer(),
                  sa.ForeignKey("users.id"), nullable=True))
    op.create_index("ix_dividend_payments_user_id", "dividend_payments", ["user_id"])


def downgrade():
    op.drop_index("ix_dividend_payments_user_id", "dividend_payments")
    op.drop_column("dividend_payments", "user_id")

    op.drop_index("ix_positions_user_id", "positions")
    op.drop_constraint("positions_pkey", "positions", type_="primary")
    op.execute("""
        ALTER TABLE positions
        ADD CONSTRAINT positions_pkey PRIMARY KEY (ticker, account)
    """)
    op.drop_column("positions", "user_id")
