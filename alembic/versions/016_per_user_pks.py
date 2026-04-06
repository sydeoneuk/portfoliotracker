"""Add per-user unique constraints to orders, transactions, dividend_payments and pies

The previous schema used T212-issued IDs as primary keys. Two users sharing the
same T212 account return identical IDs, so upserts were overwriting user_id to
whichever user synced most recently — data would move between users on every sync.

Fix: add a surrogate auto-increment PK to each affected table and replace the
T212 ID PK with a UNIQUE(user_id, t212_id) constraint so every user gets their
own independent row.

For pies the pie_holdings.pie_id FK is updated to reference the new surrogate PK.

All affected table data is cleared — it will be re-synced.

Revision ID: 016
Revises: 015
Create Date: 2026-04-06
"""
from alembic import op
import sqlalchemy as sa

revision = "016"
down_revision = "015"
branch_labels = None
depends_on = None


def upgrade():
    # ── Clear all data that will be re-synced ────────────────────────────────
    op.execute("DELETE FROM pie_holdings")
    op.execute("DELETE FROM pies")
    op.execute("DELETE FROM orders")
    op.execute("DELETE FROM transactions")
    op.execute("DELETE FROM dividend_payments")

    # ── orders ────────────────────────────────────────────────────────────────
    # Drop the T212 order ID as PK; add surrogate SERIAL PK; unique on (user_id, id)
    op.execute("ALTER TABLE orders DROP CONSTRAINT orders_pkey")
    op.execute("ALTER TABLE orders ADD COLUMN pk SERIAL PRIMARY KEY")
    op.create_unique_constraint("uq_orders_user_t212", "orders", ["user_id", "id"])

    # ── transactions ──────────────────────────────────────────────────────────
    op.execute("ALTER TABLE transactions DROP CONSTRAINT transactions_pkey")
    op.execute("ALTER TABLE transactions ADD COLUMN pk SERIAL PRIMARY KEY")
    op.create_unique_constraint("uq_transactions_user_t212", "transactions", ["user_id", "id"])

    # ── dividend_payments ─────────────────────────────────────────────────────
    # reference was the PK; add surrogate integer id PK; unique on (user_id, reference)
    op.execute("ALTER TABLE dividend_payments DROP CONSTRAINT dividend_payments_pkey")
    op.execute("ALTER TABLE dividend_payments ADD COLUMN id SERIAL PRIMARY KEY")
    op.create_unique_constraint(
        "uq_dividend_payments_user_ref", "dividend_payments", ["user_id", "reference"]
    )

    # ── pies ──────────────────────────────────────────────────────────────────
    # Drop the FK from pie_holdings before changing pies PK
    op.execute(
        "ALTER TABLE pie_holdings DROP CONSTRAINT IF EXISTS pie_holdings_pie_id_fkey"
    )
    op.execute("ALTER TABLE pies DROP CONSTRAINT pies_pkey")
    op.execute("ALTER TABLE pies ADD COLUMN pk SERIAL PRIMARY KEY")
    op.create_unique_constraint("uq_pies_user_t212", "pies", ["user_id", "id"])
    # Restore FK pointing at the new surrogate PK
    op.execute(
        "ALTER TABLE pie_holdings "
        "ADD CONSTRAINT pie_holdings_pie_id_fkey "
        "FOREIGN KEY (pie_id) REFERENCES pies(pk) ON DELETE CASCADE"
    )


def downgrade():
    raise NotImplementedError(
        "Downgrade not supported — data was cleared during upgrade and cannot be restored."
    )
