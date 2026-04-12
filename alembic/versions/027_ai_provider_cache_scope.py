"""Add provider-aware AI portfolio cache scope

Revision ID: 027
Revises: 026
Create Date: 2026-04-12
"""
from alembic import op

revision = "027"
down_revision = "026"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE ai_portfolio_analysis_cache "
        "ADD COLUMN IF NOT EXISTS provider VARCHAR(50) NOT NULL DEFAULT 'anthropic'"
    )
    op.execute(
        "ALTER TABLE ai_portfolio_analysis_cache "
        "DROP CONSTRAINT IF EXISTS uq_ai_portfolio_analysis_cache_scope"
    )
    op.execute(
        "ALTER TABLE ai_portfolio_analysis_cache "
        "ADD CONSTRAINT uq_ai_portfolio_analysis_cache_scope "
        "UNIQUE (user_id, provider, account_filter, pie_filter_key, holdings_hash)"
    )


def downgrade():
    op.execute(
        "ALTER TABLE ai_portfolio_analysis_cache "
        "DROP CONSTRAINT IF EXISTS uq_ai_portfolio_analysis_cache_scope"
    )
    op.execute(
        "ALTER TABLE ai_portfolio_analysis_cache "
        "ADD CONSTRAINT uq_ai_portfolio_analysis_cache_scope "
        "UNIQUE (user_id, account_filter, pie_filter_key, holdings_hash)"
    )
    op.execute(
        "ALTER TABLE ai_portfolio_analysis_cache "
        "DROP COLUMN IF EXISTS provider"
    )
