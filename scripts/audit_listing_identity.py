"""Read-only stable identity coverage audit. No network calls or backfill."""
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import text
from app.database import SessionLocal
from app.listing_identity import audit_summary


def main():
    with SessionLocal() as db:
        db.execute(text('SET TRANSACTION READ ONLY'))
        print(json.dumps(audit_summary(db), indent=2))
        db.rollback()


if __name__ == '__main__':
    main()
