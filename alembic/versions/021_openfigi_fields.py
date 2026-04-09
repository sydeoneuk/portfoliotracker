"""Add OpenFIGI enrichment columns to instruments table

Revision ID: 021
Revises: 020
Create Date: 2026-04-09
"""
from alembic import op

revision = "021"
down_revision = "020"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE instruments ADD COLUMN IF NOT EXISTS figi VARCHAR(20)")
    op.execute("ALTER TABLE instruments ADD COLUMN IF NOT EXISTS composite_figi VARCHAR(20)")
    op.execute("ALTER TABLE instruments ADD COLUMN IF NOT EXISTS share_class_figi VARCHAR(20)")
    op.execute("ALTER TABLE instruments ADD COLUMN IF NOT EXISTS mic_code VARCHAR(20)")
    op.execute("ALTER TABLE instruments ADD COLUMN IF NOT EXISTS security_type VARCHAR(100)")
    op.execute("ALTER TABLE instruments ADD COLUMN IF NOT EXISTS security_type2 VARCHAR(100)")
    op.execute("ALTER TABLE instruments ADD COLUMN IF NOT EXISTS market_sector VARCHAR(50)")
    op.execute("ALTER TABLE instruments ADD COLUMN IF NOT EXISTS last_figi_enriched_at TIMESTAMP")

    # Index on mic_code for exchange filtering, and figi for lookups
    op.execute("CREATE INDEX IF NOT EXISTS ix_instruments_mic_code ON instruments(mic_code)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_instruments_figi ON instruments(figi)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_instruments_security_type ON instruments(security_type)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_instruments_last_figi_enriched_at ON instruments(last_figi_enriched_at)")


def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_instruments_last_figi_enriched_at")
    op.execute("DROP INDEX IF EXISTS ix_instruments_security_type")
    op.execute("DROP INDEX IF EXISTS ix_instruments_figi")
    op.execute("DROP INDEX IF EXISTS ix_instruments_mic_code")
    op.execute("ALTER TABLE instruments DROP COLUMN IF EXISTS last_figi_enriched_at")
    op.execute("ALTER TABLE instruments DROP COLUMN IF EXISTS market_sector")
    op.execute("ALTER TABLE instruments DROP COLUMN IF EXISTS security_type2")
    op.execute("ALTER TABLE instruments DROP COLUMN IF EXISTS security_type")
    op.execute("ALTER TABLE instruments DROP COLUMN IF EXISTS mic_code")
    op.execute("ALTER TABLE instruments DROP COLUMN IF EXISTS share_class_figi")
    op.execute("ALTER TABLE instruments DROP COLUMN IF EXISTS composite_figi")
    op.execute("ALTER TABLE instruments DROP COLUMN IF EXISTS figi")
