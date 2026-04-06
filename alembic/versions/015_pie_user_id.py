"""Add user_id to pies and pie_holdings; fix unique constraint

Clears existing pie data (safe to resync) then adds user_id columns and
replaces the (pie_id, ticker) unique constraint with (user_id, pie_id, ticker).

Revision ID: 015
Revises: 014
Create Date: 2026-04-06
"""
from alembic import op
import sqlalchemy as sa

revision = "015"
down_revision = "014"
branch_labels = None
depends_on = None


def upgrade():
    # Clear existing pie data — safe because it will be re-synced.
    # pie_holdings rows cascade-delete when the parent pie row is deleted.
    op.execute("DELETE FROM pie_holdings")
    op.execute("DELETE FROM pies")

    # Add user_id to pies
    op.add_column("pies", sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True))
    op.create_index("ix_pies_user_id", "pies", ["user_id"])

    # Add user_id to pie_holdings
    op.add_column("pie_holdings", sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True))
    op.create_index("ix_pie_holdings_user_id", "pie_holdings", ["user_id"])

    # Replace the old (pie_id, ticker) unique constraint with (user_id, pie_id, ticker)
    op.drop_constraint("uq_pie_holdings_pie_ticker", "pie_holdings", type_="unique")
    op.create_unique_constraint(
        "uq_pie_holdings_user_pie_ticker",
        "pie_holdings",
        ["user_id", "pie_id", "ticker"],
    )


def downgrade():
    op.drop_constraint("uq_pie_holdings_user_pie_ticker", "pie_holdings", type_="unique")
    op.create_unique_constraint("uq_pie_holdings_pie_ticker", "pie_holdings", ["pie_id", "ticker"])
    op.drop_index("ix_pie_holdings_user_id", "pie_holdings")
    op.drop_column("pie_holdings", "user_id")
    op.drop_index("ix_pies_user_id", "pies")
    op.drop_column("pies", "user_id")
