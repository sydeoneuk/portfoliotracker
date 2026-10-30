"""Backfill platform rows from existing account data and rebuild constraints

Revision ID: 033
Revises: 032
Create Date: 2026-10-30

Strategy:
  1. Collect every (user_id, account) pair across all tables, create a Platform row for each.
  2. Backfill platform_id on every table using the new rows.
  3. Rebuild the positions primary key from (ticker, account, user_id) to
     (ticker, platform_id, user_id) — platform_id becomes NOT NULL on positions.
  4. Replace account-keyed unique constraints on portfolio/holding snapshots with
     platform_id-keyed ones.
  5. account columns remain as nullable deprecated columns; they are dropped in a
     future cleanup migration once the web UI is fully migrated.
"""
from alembic import op


def upgrade():
    # ------------------------------------------------------------------
    # 1. Create one Platform row per unique (user_id, account) pair found
    #    across positions, orders, dividend_payments, and pies.
    # ------------------------------------------------------------------
    op.execute("""
        WITH all_accounts AS (
            SELECT DISTINCT user_id, account FROM positions
                WHERE account IS NOT NULL AND user_id IS NOT NULL
            UNION
            SELECT DISTINCT user_id, account FROM orders
                WHERE account IS NOT NULL AND user_id IS NOT NULL
            UNION
            SELECT DISTINCT user_id, account FROM dividend_payments
                WHERE account IS NOT NULL AND user_id IS NOT NULL
            UNION
            SELECT DISTINCT user_id, account FROM pies
                WHERE account IS NOT NULL AND user_id IS NOT NULL
        )
        INSERT INTO platforms (user_id, name, platform_type, account_type, currency, is_active, created_at, updated_at)
        SELECT
            user_id,
            CASE account
                WHEN 'ISA'     THEN 'Trading 212 ISA'
                WHEN 'Trading' THEN 'Trading 212 Invest'
                ELSE 'Trading 212 ' || account
            END,
            't212_api',
            CASE account
                WHEN 'ISA'     THEN 'ISA'
                WHEN 'Trading' THEN 'GIA'
                ELSE account
            END,
            'GBP',
            true,
            NOW(),
            NOW()
        FROM all_accounts
        ON CONFLICT DO NOTHING
    """)

    # ------------------------------------------------------------------
    # 2. Backfill platform_id on each table
    # ------------------------------------------------------------------
    _backfill = """
        UPDATE {table} t
        SET platform_id = pl.id
        FROM platforms pl
        WHERE pl.user_id = t.user_id
          AND pl.platform_type = 't212_api'
          AND pl.account_type = CASE t.account
                WHEN 'ISA'     THEN 'ISA'
                WHEN 'Trading' THEN 'GIA'
                ELSE t.account
              END
          AND t.platform_id IS NULL
          AND t.account IS NOT NULL
    """
    for table in ("positions", "orders", "dividend_payments", "pies",
                  "portfolio_snapshots", "holding_snapshots",
                  "pie_holding_snapshots", "dividend_payment_allocations"):
        op.execute(_backfill.format(table=table))

    # transactions has no account column — leave platform_id NULL for existing rows
    # (they will be assigned when the next API sync runs)

    # ------------------------------------------------------------------
    # 3. Rebuild the positions primary key
    #    Old PK: (ticker, account, user_id)
    #    New PK: (ticker, platform_id, user_id)
    # ------------------------------------------------------------------
    op.execute("""
        ALTER TABLE positions DROP CONSTRAINT IF EXISTS positions_pkey
    """)
    op.execute("""
        ALTER TABLE positions ALTER COLUMN platform_id SET NOT NULL
    """)
    op.execute("""
        ALTER TABLE positions ADD PRIMARY KEY (ticker, platform_id, user_id)
    """)
    # account is no longer part of the PK; make it fully optional
    op.execute("""
        ALTER TABLE positions ALTER COLUMN account DROP NOT NULL
    """)

    # ------------------------------------------------------------------
    # 4. Replace account-keyed unique constraints on snapshot tables
    # ------------------------------------------------------------------

    # portfolio_snapshots
    op.execute("""
        ALTER TABLE portfolio_snapshots
        DROP CONSTRAINT IF EXISTS uq_portfolio_snapshots_daily_scope
    """)
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_portfolio_snapshots_daily_scope
        ON portfolio_snapshots (user_id, snapshot_date, platform_id, scope_type, COALESCE(pie_id, 0))
    """)
    op.execute("""
        ALTER TABLE portfolio_snapshots ALTER COLUMN account DROP NOT NULL
    """)

    # holding_snapshots
    op.execute("""
        ALTER TABLE holding_snapshots
        DROP CONSTRAINT IF EXISTS uq_holding_snapshots_daily_ticker
    """)
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_holding_snapshots_daily_ticker
        ON holding_snapshots (user_id, snapshot_date, platform_id, ticker)
    """)
    op.execute("""
        ALTER TABLE holding_snapshots ALTER COLUMN account DROP NOT NULL
    """)

    # dividend_payments: account was NOT NULL — relax it
    op.execute("""
        ALTER TABLE dividend_payments ALTER COLUMN account DROP NOT NULL
    """)

    # dividend_payment_allocations: account was NOT NULL — relax it
    op.execute("""
        ALTER TABLE dividend_payment_allocations ALTER COLUMN account DROP NOT NULL
    """)


def downgrade():
    # Restore positions PK (requires platform_id → account backfill which we skip)
    op.execute("ALTER TABLE positions DROP CONSTRAINT IF EXISTS positions_pkey")
    op.execute("ALTER TABLE positions ADD PRIMARY KEY (ticker, account, user_id)")
    op.execute("ALTER TABLE positions ALTER COLUMN platform_id DROP NOT NULL")

    # Restore snapshot constraints
    op.execute("DROP INDEX IF EXISTS uq_portfolio_snapshots_daily_scope")
    op.execute("""
        ALTER TABLE portfolio_snapshots
        ADD CONSTRAINT uq_portfolio_snapshots_daily_scope
        UNIQUE (user_id, snapshot_date, account, scope_type, pie_id)
    """)
    op.execute("ALTER TABLE portfolio_snapshots ALTER COLUMN account SET NOT NULL")

    op.execute("DROP INDEX IF EXISTS uq_holding_snapshots_daily_ticker")
    op.execute("""
        ALTER TABLE holding_snapshots
        ADD CONSTRAINT uq_holding_snapshots_daily_ticker
        UNIQUE (user_id, snapshot_date, account, ticker)
    """)
    op.execute("ALTER TABLE holding_snapshots ALTER COLUMN account SET NOT NULL")

    op.execute("ALTER TABLE dividend_payments ALTER COLUMN account SET NOT NULL")
    op.execute("ALTER TABLE dividend_payment_allocations ALTER COLUMN account SET NOT NULL")
