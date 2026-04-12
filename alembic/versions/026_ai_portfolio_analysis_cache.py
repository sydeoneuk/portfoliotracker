"""Add AI portfolio analysis cache table

Revision ID: 026
Revises: 025
Create Date: 2026-04-12
"""
from alembic import op
import sqlalchemy as sa

revision = "026"
down_revision = "025"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "ai_portfolio_analysis_cache",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("account_filter", sa.String(length=20), nullable=False),
        sa.Column("pie_filter_key", sa.String(length=255), nullable=False),
        sa.Column("holdings_hash", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("analysis_text", sa.Text(), nullable=False),
        sa.Column("holdings_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "user_id",
            "account_filter",
            "pie_filter_key",
            "holdings_hash",
            name="uq_ai_portfolio_analysis_cache_scope",
        ),
    )
    op.create_index(
        "ix_ai_portfolio_analysis_cache_user_id",
        "ai_portfolio_analysis_cache",
        ["user_id"],
    )


def downgrade():
    op.drop_index("ix_ai_portfolio_analysis_cache_user_id", table_name="ai_portfolio_analysis_cache")
    op.drop_table("ai_portfolio_analysis_cache")
