"""Scope provider identifiers to accounts without changing internal IDs or links."""
from alembic import op
import sqlalchemy as sa

revision = "041"
down_revision = "040"
branch_labels = None
depends_on = None

TABLES = (
    ("orders", "id", "uq_orders_user_t212"),
    ("transactions", "id", "uq_transactions_user_t212"),
    ("dividend_payments", "reference", "uq_dividend_payments_user_ref"),
    ("pies", "id", "uq_pies_user_t212"),
)


def upgrade():
    connection = op.get_bind()
    # Ownership problems must be reconciled, never silently reassigned.
    for table, _, _ in TABLES:
        mismatches = connection.execute(sa.text(f"""
            SELECT COUNT(*) FROM {table} r JOIN platforms p ON p.id = r.platform_id
            WHERE r.user_id IS DISTINCT FROM p.user_id
        """)).scalar_one()
        if mismatches:
            raise RuntimeError(f"{table}: {mismatches} account ownership conflicts; no records changed")

    op.create_unique_constraint("uq_platforms_id_user", "platforms", ["id", "user_id"])
    for table, key, old_constraint in TABLES:
        op.add_column(table, sa.Column("source_namespace", sa.String(80), nullable=False, server_default="legacy"))
        namespace = """CASE
            WHEN p.platform_type IN ('t212_api', 't212_csv') THEN 't212'
            WHEN p.platform_type = 'interactive_investor' THEN 'ii'
            WHEN p.platform_type IN ('charles_stanley', 'best_invest') THEN p.platform_type
            ELSE 'legacy' END"""
        if table == "pies":
            namespace = """CASE WHEN p.platform_type = 't212_api' THEN 't212'
                WHEN p.platform_type IN ('t212_csv', 'interactive_investor', 'charles_stanley', 'best_invest')
                THEN 't212_csv_pie' ELSE 'legacy' END"""
        op.execute(f"UPDATE {table} r SET source_namespace = {namespace} FROM platforms p WHERE p.id = r.platform_id")
        op.create_check_constraint(f"ck_{table}_resolved_source_owner", table,
                                   "source_namespace = 'legacy' OR (user_id IS NOT NULL AND platform_id IS NOT NULL)")
        op.create_unique_constraint(f"uq_{table}_account_source", table,
                                    ["user_id", "platform_id", "source_namespace", key])
        # Unassigned rows retain a uniqueness boundary and remain unassigned.
        op.execute(f"""CREATE UNIQUE INDEX uq_{table}_unassigned_source
            ON {table} (user_id, source_namespace, {key}) WHERE platform_id IS NULL""")
        op.create_foreign_key(f"fk_{table}_account_owner", table, "platforms",
                              ["platform_id", "user_id"], ["id", "user_id"])
        op.drop_constraint(old_constraint, table, type_="unique")


def downgrade():
    raise RuntimeError("Keep account-scoped identities; restoring old uniqueness could discard legitimate account records.")
