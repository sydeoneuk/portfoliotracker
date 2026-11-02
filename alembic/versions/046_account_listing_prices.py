"""Add derived verified account quotes without rewriting legacy data."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = '046'
down_revision = '045'
branch_labels = depends_on = None


def upgrade():
    op.create_table('account_listing_prices',
        sa.Column('resolution_id', sa.Integer(), sa.ForeignKey('account_listing_resolutions.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('resolution_revision', sa.Integer(), nullable=False),
        sa.Column('source_fingerprint', sa.String(64), nullable=False),
        sa.Column('authority_batch_id', sa.Integer(), sa.ForeignKey('ingestion_batches.id', ondelete='CASCADE')),
        sa.Column('catalogue_metadata', JSONB, nullable=False),
        sa.Column('raw_price', sa.Numeric(28, 10)), sa.Column('quote_currency', sa.String(10)),
        sa.Column('unit_scale', sa.Numeric(12, 8)), sa.Column('normalized_price', sa.Numeric(28, 10)),
        sa.Column('normalized_currency', sa.String(10)), sa.Column('conversion_rate', sa.Numeric(28, 14)),
        sa.Column('observed_at', sa.DateTime()), sa.Column('fetched_at', sa.DateTime()),
        sa.Column('expires_at', sa.DateTime()), sa.Column('checked_at', sa.DateTime(), nullable=False),
        sa.Column('error', sa.String(500)))


def downgrade():
    raise RuntimeError('Retain quote provenance; roll back application code without dropping data')
