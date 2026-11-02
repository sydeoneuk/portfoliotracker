"""Add retained uploads, holding observations and explicit import authority."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = '043'
down_revision = '042'
branch_labels = depends_on = None


def upgrade():
    op.create_table('ingestion_batches',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('platform_id', sa.Integer(), sa.ForeignKey('platforms.id'), nullable=False),
        sa.Column('source', sa.String(40), nullable=False),
        sa.Column('filename', sa.String(255), nullable=False),
        sa.Column('checksum', sa.String(64), nullable=False),
        sa.Column('parser_version', sa.String(30), nullable=False),
        sa.Column('raw_content', sa.LargeBinary(), nullable=False),
        sa.Column('effective_date', sa.Date()),
        sa.Column('history_complete', sa.Boolean(), nullable=False),
        sa.Column('options', JSONB, nullable=False),
        sa.Column('status', sa.String(30), nullable=False),
        sa.Column('baseline', sa.String(64)),
        sa.Column('review', JSONB, nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('applied_at', sa.DateTime()),
        sa.ForeignKeyConstraint(['platform_id', 'user_id'], ['platforms.id', 'platforms.user_id']), sa.UniqueConstraint('id', 'user_id'))
    for col in ('user_id', 'platform_id', 'checksum'):
        op.create_index('ix_ingestion_batches_' + col, 'ingestion_batches', [col])
    op.create_table('ingestion_rows',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('batch_id', sa.Integer(), sa.ForeignKey('ingestion_batches.id'), nullable=False),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('row_number', sa.Integer(), nullable=False),
        sa.Column('cells', JSONB, nullable=False), sa.UniqueConstraint('batch_id', 'row_number'),
        sa.ForeignKeyConstraint(['batch_id', 'user_id'], ['ingestion_batches.id', 'ingestion_batches.user_id']))
    op.create_index('ix_ingestion_rows_batch_id', 'ingestion_rows', ['batch_id'])
    op.create_table('import_holding_observations',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('batch_id', sa.Integer(), sa.ForeignKey('ingestion_batches.id'), nullable=False),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('phase', sa.String(20), nullable=False),
        sa.Column('scope', sa.String(180), nullable=False),
        sa.Column('effective_date', sa.Date()), sa.Column('complete', sa.Boolean(), nullable=False),
        sa.Column('holdings', JSONB, nullable=False),
        sa.ForeignKeyConstraint(['batch_id', 'user_id'], ['ingestion_batches.id', 'ingestion_batches.user_id']))
    op.create_index('ix_import_holding_observations_batch_id', 'import_holding_observations', ['batch_id'])
    op.create_table('import_authorities',
        sa.Column('platform_id', sa.Integer(), sa.ForeignKey('platforms.id'), primary_key=True),
        sa.Column('scope', sa.String(180), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('batch_id', sa.Integer(), sa.ForeignKey('ingestion_batches.id'), nullable=False),
        sa.Column('effective_date', sa.Date(), nullable=False),
        sa.ForeignKeyConstraint(['platform_id', 'user_id'], ['platforms.id', 'platforms.user_id']),
        sa.ForeignKeyConstraint(['batch_id', 'user_id'], ['ingestion_batches.id', 'ingestion_batches.user_id']))


def downgrade():
    raise RuntimeError('Retained ingestion evidence must be preserved; use a compatible application rollback')
