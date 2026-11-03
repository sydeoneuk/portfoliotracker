"""Freeze units and valuation evidence on new holding snapshots."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = '047'
down_revision = '046'
branch_labels = depends_on = None


def upgrade():
    # No backfill: today's catalogue cannot establish historical quote units.
    op.add_column('holding_snapshots', sa.Column('price_currency', sa.String(10)))
    op.add_column('holding_snapshots', sa.Column('price_unit_scale', sa.Numeric(12, 8)))
    op.add_column('holding_snapshots', sa.Column('fx_rate_to_gbp', sa.Numeric(28, 14)))
    op.add_column('holding_snapshots', sa.Column('valuation_evidence', JSONB))


def downgrade():
    raise RuntimeError('Preserve snapshot evidence; roll back application code without dropping data')
