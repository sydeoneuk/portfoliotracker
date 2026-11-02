"""Add explicit execution currencies and separate cost projections; retain legacy values."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "040"
down_revision = "039"
branch_labels = None
depends_on = None


def upgrade():
    for column in (
        sa.Column("execution_price", sa.Numeric(28, 10)),
        sa.Column("execution_currency", sa.String(10)),
        sa.Column("settlement_amount", sa.Numeric(28, 10)),
        sa.Column("settlement_currency", sa.String(10)),
        sa.Column("settlement_date", sa.Date()),
        sa.Column("execution_fx_rate", sa.Numeric(28, 12)),
        sa.Column("charges", JSONB),
    ):
        op.add_column("orders", column)
    for column in (
        sa.Column("cost_basis_amount", sa.Numeric(28, 10)),
        sa.Column("cost_basis_currency", sa.String(10)),
        sa.Column("cost_basis_status", sa.String(30)),
        sa.Column("cost_basis_source", sa.String(30)),
        sa.Column("cost_basis_reason", sa.Text()),
    ):
        op.add_column("positions", column)
    op.create_table(
        "trade_import_observations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("order_pk", sa.Integer(), sa.ForeignKey("orders.pk", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("imported_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("order_pk", "payload_hash", name="uq_trade_observation_payload"),
    )
    op.create_index("ix_trade_import_observations_user_id", "trade_import_observations", ["user_id"])


def downgrade():
    raise RuntimeError("Retain trade provenance and original values; use application rollback without schema downgrade.")
