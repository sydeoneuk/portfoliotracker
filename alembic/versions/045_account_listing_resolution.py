"""Add account/source-scoped listing resolutions and restartable audit history."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = '045'
down_revision = '044'
branch_labels = depends_on = None


def upgrade():
    op.create_table('account_listing_resolutions',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('platform_id', sa.Integer(), sa.ForeignKey('platforms.id'), nullable=False),
        sa.Column('source_namespace', sa.String(120), nullable=False),
        sa.Column('ticker', sa.String(), sa.ForeignKey('instruments.ticker'), nullable=False),
        sa.Column('listing_id', sa.Integer(), sa.ForeignKey('listings.id'), nullable=False),
        sa.Column('provider_mapping_id', sa.Integer(), sa.ForeignKey('provider_listing_mappings.id'), nullable=False),
        sa.Column('source_fingerprint', sa.String(64), nullable=False),
        sa.Column('claim', JSONB, nullable=False), sa.Column('evidence', JSONB, nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False), sa.Column('verified_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('platform_id', 'source_namespace', 'ticker', name='uq_account_listing_scope'),
        sa.ForeignKeyConstraint(['platform_id', 'user_id'], ['platforms.id', 'platforms.user_id']),
        sa.ForeignKeyConstraint(['provider_mapping_id', 'listing_id'], ['provider_listing_mappings.id', 'provider_listing_mappings.listing_id']))
    op.create_index('ix_account_listing_resolution_owner', 'account_listing_resolutions', ['user_id', 'platform_id'])
    op.create_table('account_listing_audits',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('actor_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL')),
        sa.Column('platform_id', sa.Integer(), sa.ForeignKey('platforms.id'), nullable=False),
        sa.Column('source_namespace', sa.String(120), nullable=False), sa.Column('ticker', sa.String(), nullable=False),
        sa.Column('action', sa.String(20), nullable=False), sa.Column('review_key', sa.String(36), unique=True),
        sa.Column('before', JSONB), sa.Column('after', JSONB),
        sa.Column('undo_of', sa.Integer(), sa.ForeignKey('account_listing_audits.id'), unique=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['platform_id', 'user_id'], ['platforms.id', 'platforms.user_id']))
    op.create_index('ix_account_listing_audit_scope', 'account_listing_audits', ['platform_id', 'source_namespace', 'ticker', 'id'])


def downgrade():
    raise RuntimeError('Retain scoped resolutions and their evidence; use a compatible application rollback')
