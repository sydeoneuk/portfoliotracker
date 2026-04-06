"""add users and user_settings tables

Revision ID: 008
Revises: 007
Create Date: 2026-04-05
"""
from alembic import op
import sqlalchemy as sa

revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("email", sa.String(255), unique=True, nullable=False),
        sa.Column("name", sa.String(255)),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("provider_id", sa.String(255), nullable=False),
        sa.Column("avatar_url", sa.String(500)),
        sa.Column("created_at", sa.DateTime()),
        sa.Column("last_login_at", sa.DateTime()),
    )
    op.create_index("ix_users_email", "users", ["email"])

    op.create_table(
        "user_settings",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("t212_api_key_enc", sa.Text()),
        sa.Column("t212_isa_api_key_enc", sa.Text()),
        sa.Column("last_sync_at", sa.DateTime()),
        sa.Column("sync_status", sa.String(20), server_default="idle"),
        sa.Column("sync_message", sa.Text()),
    )


def downgrade():
    op.drop_table("user_settings")
    op.drop_table("users")
