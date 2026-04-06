"""initial schema

Revision ID: 001
Revises:
Create Date: 2026-03-29
"""
from alembic import op
import sqlalchemy as sa

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "instruments",
        sa.Column("ticker", sa.String(), primary_key=True),
        sa.Column("name", sa.String()),
        sa.Column("short_name", sa.String()),
        sa.Column("currency_code", sa.String(10)),
        sa.Column("isin", sa.String(20)),
        sa.Column("instrument_type", sa.String(50)),
        sa.Column("exchange", sa.String(100)),
        sa.Column("min_trade_quantity", sa.Float()),
        sa.Column("max_open_quantity", sa.Float()),
        sa.Column("sector", sa.String(100)),
        sa.Column("industry", sa.String(100)),
        sa.Column("market_cap", sa.Float()),
        sa.Column("description", sa.String()),
        sa.Column("country", sa.String(100)),
        sa.Column("last_enriched_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime()),
        sa.Column("updated_at", sa.DateTime()),
    )

    op.create_table(
        "pies",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("icon", sa.String()),
        sa.Column("goal", sa.Float()),
        sa.Column("creation_date", sa.DateTime()),
        sa.Column("end_date", sa.DateTime()),
        sa.Column("initial_investment", sa.Float()),
        sa.Column("dividend_cash_action", sa.String(20)),
        sa.Column("last_synced_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime()),
    )

    op.create_table(
        "positions",
        sa.Column("ticker", sa.String(), sa.ForeignKey("instruments.ticker"), primary_key=True),
        sa.Column("quantity", sa.Float()),
        sa.Column("average_price", sa.Float()),
        sa.Column("current_price", sa.Float()),
        sa.Column("ppl", sa.Float()),
        sa.Column("fx_ppl", sa.Float()),
        sa.Column("result_coef", sa.Float()),
        sa.Column("last_synced_at", sa.DateTime()),
    )

    op.create_table(
        "orders",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("ticker", sa.String(), sa.ForeignKey("instruments.ticker")),
        sa.Column("quantity", sa.Float()),
        sa.Column("filled_quantity", sa.Float()),
        sa.Column("order_type", sa.String(20)),
        sa.Column("status", sa.String(20)),
        sa.Column("limit_price", sa.Float()),
        sa.Column("stop_price", sa.Float()),
        sa.Column("fill_price", sa.Float()),
        sa.Column("time_validity", sa.String(10)),
        sa.Column("created_at", sa.DateTime()),
        sa.Column("filled_at", sa.DateTime()),
        sa.Column("synced_at", sa.DateTime()),
    )

    op.create_table(
        "transactions",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("type", sa.String(50)),
        sa.Column("amount", sa.Float()),
        sa.Column("date_time", sa.DateTime()),
        sa.Column("reference", sa.String()),
        sa.Column("notes", sa.String()),
        sa.Column("synced_at", sa.DateTime()),
    )

    op.create_table(
        "pie_holdings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("pie_id", sa.Integer(), sa.ForeignKey("pies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ticker", sa.String(), sa.ForeignKey("instruments.ticker"), nullable=False),
        sa.Column("expected_share", sa.Float()),
        sa.Column("current_share", sa.Float()),
        sa.Column("owned_quantity", sa.Float()),
        sa.Column("price_avg_invested_value", sa.Float()),
        sa.Column("price_avg_value", sa.Float()),
        sa.Column("price_avg_result", sa.Float()),
        sa.Column("price_avg_result_coef", sa.Float()),
        sa.Column("synced_at", sa.DateTime()),
        sa.UniqueConstraint("pie_id", "ticker", name="uq_pie_holdings_pie_ticker"),
    )

    op.create_index("ix_positions_ticker", "positions", ["ticker"])
    op.create_index("ix_orders_ticker", "orders", ["ticker"])
    op.create_index("ix_orders_status", "orders", ["status"])
    op.create_index("ix_transactions_date_time", "transactions", ["date_time"])
    op.create_index("ix_pie_holdings_pie_id", "pie_holdings", ["pie_id"])


def downgrade() -> None:
    op.drop_table("pie_holdings")
    op.drop_table("transactions")
    op.drop_table("orders")
    op.drop_table("positions")
    op.drop_table("pies")
    op.drop_table("instruments")
