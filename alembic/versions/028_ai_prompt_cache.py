"""Store AI analysis prompt text in cache

Revision ID: 028
Revises: 027
Create Date: 2026-04-12
"""
from alembic import op

revision = "028"
down_revision = "027"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE ai_portfolio_analysis_cache "
        "ADD COLUMN IF NOT EXISTS prompt_text TEXT"
    )


def downgrade():
    op.execute(
        "ALTER TABLE ai_portfolio_analysis_cache "
        "DROP COLUMN IF EXISTS prompt_text"
    )
