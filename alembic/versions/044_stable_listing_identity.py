"""Add stable security/listing IDs and audited verified legacy alias links."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = '044'
down_revision = '043'
branch_labels = depends_on = None


def upgrade():
    op.create_table('securities', sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('isin', sa.String(12), nullable=False, unique=True),
        sa.Column('created_at', sa.DateTime(), nullable=False))
    op.create_table('listings', sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('security_id', sa.Integer(), sa.ForeignKey('securities.id'), nullable=False),
        sa.Column('mic', sa.String(4), nullable=False), sa.Column('quote_currency', sa.String(3), nullable=False),
        sa.Column('unit_scale', sa.Numeric(12, 8), nullable=False), sa.Column('venue_symbol', sa.String(40), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('security_id', 'mic', 'quote_currency', 'unit_scale', 'venue_symbol', name='uq_listing_identity'),
        sa.CheckConstraint('unit_scale > 0', name='ck_listing_unit_scale'))
    op.create_table('provider_listing_mappings', sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('provider', sa.String(40), nullable=False), sa.Column('namespace', sa.String(40), nullable=False),
        sa.Column('identifier', sa.String(80), nullable=False),
        sa.Column('listing_id', sa.Integer(), sa.ForeignKey('listings.id'), nullable=False),
        sa.Column('evidence', JSONB, nullable=False), sa.Column('verified_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('provider', 'namespace', 'identifier', name='uq_provider_listing_identifier'),
        sa.UniqueConstraint('id', 'listing_id', name='uq_provider_mapping_listing'))
    op.create_table('instrument_listing_links', sa.Column('ticker', sa.String(), sa.ForeignKey('instruments.ticker'), primary_key=True),
        sa.Column('listing_id', sa.Integer(), sa.ForeignKey('listings.id'), nullable=False),
        sa.Column('provider_mapping_id', sa.Integer(), sa.ForeignKey('provider_listing_mappings.id'), nullable=False),
        sa.Column('source_fingerprint', sa.String(64), nullable=False), sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('evidence', JSONB, nullable=False), sa.Column('verified_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['provider_mapping_id', 'listing_id'], ['provider_listing_mappings.id', 'provider_listing_mappings.listing_id']))
    op.create_index('ix_instrument_listing_links_listing', 'instrument_listing_links', ['listing_id'])
    op.create_table('listing_identity_audits', sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL')),
        sa.Column('ticker', sa.String(), nullable=False), sa.Column('action', sa.String(20), nullable=False),
        sa.Column('before', JSONB), sa.Column('after', JSONB),
        sa.Column('undo_of', sa.Integer(), sa.ForeignKey('listing_identity_audits.id'), unique=True),
        sa.Column('created_at', sa.DateTime(), nullable=False))
    op.create_index('ix_listing_identity_audits_ticker', 'listing_identity_audits', ['ticker', 'id'])


def downgrade():
    raise RuntimeError('Stable identity and audit records must be retained; use a compatible application rollback')
