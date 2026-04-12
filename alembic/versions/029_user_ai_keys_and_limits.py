"""Add user AI keys, app settings, and AI analysis usage

Revision ID: 029
Revises: 028
Create Date: 2026-04-12
"""
from alembic import op
import sqlalchemy as sa

revision = "029"
down_revision = "028"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS anthropic_api_key_enc TEXT")
    op.execute("ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS openai_api_key_enc TEXT")

    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(length=100), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )

    op.create_table(
        "ai_analysis_usage",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("used_shared_key", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_ai_analysis_usage_user_id", "ai_analysis_usage", ["user_id"])
    op.create_index("ix_ai_analysis_usage_created_at", "ai_analysis_usage", ["created_at"])

    op.execute(
        "INSERT INTO app_settings (key, value, updated_at) "
        "VALUES ('shared_ai_daily_limit', '3', NOW()) "
        "ON CONFLICT (key) DO NOTHING"
    )


def downgrade():
    op.drop_index("ix_ai_analysis_usage_created_at", table_name="ai_analysis_usage")
    op.drop_index("ix_ai_analysis_usage_user_id", table_name="ai_analysis_usage")
    op.drop_table("ai_analysis_usage")
    op.drop_table("app_settings")
    op.execute("ALTER TABLE user_settings DROP COLUMN IF EXISTS openai_api_key_enc")
    op.execute("ALTER TABLE user_settings DROP COLUMN IF EXISTS anthropic_api_key_enc")
