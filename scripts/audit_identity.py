"""Read-only pre/post-migration identity audit; prints counts, never credentials or raw IDs."""
import json
from pathlib import Path
import sys

from sqlalchemy import inspect, text

TABLES = ("orders", "transactions", "dividend_payments", "pies")


def audit_identity(connection):
    inspector = inspect(connection)
    report = {}
    for table in TABLES:
        def count(where):
            return connection.execute(text(f"SELECT COUNT(*) FROM {table} r LEFT JOIN platforms p ON p.id=r.platform_id WHERE {where}")).scalar_one()
        record = {
            "rows": count("true"),
            "unassigned_account": count("r.platform_id IS NULL"),
            "missing_user": count("r.user_id IS NULL"),
            "ownership_conflicts": count("r.platform_id IS NOT NULL AND (p.id IS NULL OR r.user_id IS DISTINCT FROM p.user_id)"),
        }
        if any(column["name"] == "source_namespace" for column in inspector.get_columns(table)):
            record["legacy_source"] = count("r.source_namespace = 'legacy'")
        else:
            record["source_namespace"] = "not yet migrated"
        report[table] = record
    return report


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.database import engine
    with engine.connect() as connection:
        report = audit_identity(connection)
    print(json.dumps(report, indent=2))
    return 1 if any(r["ownership_conflicts"] for r in report.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
