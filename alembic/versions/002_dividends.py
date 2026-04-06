"""dividend history and forecast tables

Revision ID: 002
Revises: 001
Create Date: 2026-03-29
"""
from alembic import op
import sqlalchemy as sa

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add yf_ticker + last_dividend_synced_at to instruments
    op.add_column("instruments", sa.Column("yf_ticker", sa.String(30)))
    op.add_column("instruments", sa.Column("last_dividend_synced_at", sa.DateTime()))

    op.create_table(
        "dividend_history",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ticker", sa.String(), sa.ForeignKey("instruments.ticker"), nullable=False),
        sa.Column("ex_date", sa.Date(), nullable=False),
        sa.Column("pay_date", sa.Date()),
        sa.Column("record_date", sa.Date()),
        sa.Column("declaration_date", sa.Date()),
        sa.Column("amount", sa.Float(), nullable=False),
        sa.Column("adj_amount", sa.Float()),
        sa.Column("currency", sa.String(10)),
        sa.Column("source", sa.String(50)),
        sa.Column("fetched_at", sa.DateTime()),
        sa.UniqueConstraint("ticker", "ex_date", name="uq_dividend_history_ticker_exdate"),
    )
    op.create_index("ix_dividend_history_ticker", "dividend_history", ["ticker"])
    op.create_index("ix_dividend_history_ex_date", "dividend_history", ["ex_date"])

    op.create_table(
        "dividend_forecast",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ticker", sa.String(), sa.ForeignKey("instruments.ticker"), nullable=False),
        sa.Column("ex_date", sa.Date()),
        sa.Column("pay_date", sa.Date()),
        sa.Column("amount", sa.Float()),
        sa.Column("is_estimated", sa.Boolean(), default=True),
        sa.Column("frequency", sa.String(20)),
        sa.Column("annual_rate", sa.Float()),
        sa.Column("dividend_yield", sa.Float()),
        sa.Column("source", sa.String(50)),
        sa.Column("fetched_at", sa.DateTime()),
        sa.UniqueConstraint("ticker", "ex_date", name="uq_dividend_forecast_ticker_exdate"),
    )
    op.create_index("ix_dividend_forecast_ticker", "dividend_forecast", ["ticker"])
    op.create_index("ix_dividend_forecast_ex_date", "dividend_forecast", ["ex_date"])


def downgrade() -> None:
    op.drop_table("dividend_forecast")
    op.drop_table("dividend_history")
    op.drop_column("instruments", "last_dividend_synced_at")
    op.drop_column("instruments", "yf_ticker")
