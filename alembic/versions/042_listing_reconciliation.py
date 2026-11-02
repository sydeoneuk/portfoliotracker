"""Add user-specific verified price mappings, proposals and immutable change history."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "042"
down_revision = "041"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "user_instrument_mappings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ticker", sa.String(), sa.ForeignKey("instruments.ticker"), nullable=False),
        sa.Column("isin", sa.String(20), nullable=False),
        sa.Column("listing_currency", sa.String(10), nullable=False),
        sa.Column("provider_symbol", sa.String(80), nullable=False),
        sa.Column("quote_currency", sa.String(10), nullable=False),
        sa.Column("exchange", sa.String(100)),
        sa.Column("evidence", JSONB, nullable=False),
        sa.Column("price", sa.Numeric(28, 10)),
        sa.Column("normalized_price", sa.Numeric(28, 10)),
        sa.Column("conversion_rate", sa.Numeric(28, 12)),
        sa.Column("price_observed_at", sa.DateTime()),
        sa.Column("checked_at", sa.DateTime(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.UniqueConstraint("user_id", "ticker", name="uq_user_mapping_ticker"),
    )
    op.create_index("ix_user_instrument_mappings_user_id", "user_instrument_mappings", ["user_id"])
    op.create_table(
        "instrument_mapping_proposals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ticker", sa.String(), sa.ForeignKey("instruments.ticker"), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("baseline", JSONB, nullable=False),
        sa.Column("candidate", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_instrument_mapping_proposals_user_id", "instrument_mapping_proposals", ["user_id"])
    op.create_table(
        "instrument_mapping_audits",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ticker", sa.String(), sa.ForeignKey("instruments.ticker"), nullable=False),
        sa.Column("proposal_id", sa.Integer(), sa.ForeignKey("instrument_mapping_proposals.id")),
        sa.Column("action", sa.String(20), nullable=False),
        sa.Column("before", JSONB),
        sa.Column("after", JSONB),
        sa.Column("undo_of", sa.Integer(), sa.ForeignKey("instrument_mapping_audits.id"), unique=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_instrument_mapping_audits_user_id", "instrument_mapping_audits", ["user_id"])


def downgrade():
    raise RuntimeError("Retain verified mappings and audit history; use a compatible application rollback.")
