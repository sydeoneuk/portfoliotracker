"""Add indexes to instruments table for filtering and search

Revision ID: 020
Revises: 019
Create Date: 2026-04-08
"""
from alembic import op

revision = "020"
down_revision = "019"
branch_labels = None
depends_on = None


def upgrade():
    # Enrichment / filter columns — used in WHERE clauses on the research page
    op.execute("CREATE INDEX IF NOT EXISTS ix_instruments_sector ON instruments(sector)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_instruments_country ON instruments(country)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_instruments_exchange ON instruments(exchange)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_instruments_instrument_type ON instruments(instrument_type)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_instruments_last_enriched_at ON instruments(last_enriched_at)")

    # Name / short_name — used for text search (ILIKE)
    op.execute("CREATE INDEX IF NOT EXISTS ix_instruments_name ON instruments(name)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_instruments_short_name ON instruments(short_name)")

    # Composite: most common multi-column filter (country + sector)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_instruments_country_sector "
        "ON instruments(country, sector)"
    )


def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_instruments_country_sector")
    op.execute("DROP INDEX IF EXISTS ix_instruments_short_name")
    op.execute("DROP INDEX IF EXISTS ix_instruments_name")
    op.execute("DROP INDEX IF EXISTS ix_instruments_last_enriched_at")
    op.execute("DROP INDEX IF EXISTS ix_instruments_instrument_type")
    op.execute("DROP INDEX IF EXISTS ix_instruments_exchange")
    op.execute("DROP INDEX IF EXISTS ix_instruments_country")
    op.execute("DROP INDEX IF EXISTS ix_instruments_sector")
