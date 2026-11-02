"""Read-only account/source identity coverage audit; no network or backfill."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sqlalchemy import text
from app.database import SessionLocal
from app.scoped_identity import audit_report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--search',default='',help='Filter by account name, alias or catalogue ISIN')
    parser.add_argument('--include-historical',action='store_true')
    args=parser.parse_args()
    counts={'unresolved':0,'verified':0,'stale':0,'conflict':0,'alias_conflict_scopes':0}
    offset=0
    with SessionLocal() as db:
        db.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
        while True:
            report=audit_report(db,args.search,offset,not args.include_historical)
            for row in report['items']:
                counts[row['status']]+=1
                counts['alias_conflict_scopes']+=int(row['legacy_alias_conflict'])
            offset+=len(report['items'])
            if offset>=report['total'] or not report['items']:
                break
        counts.update(total_scopes=report['total'],scanned_scopes=offset,unassigned_event_records=report['unassigned_event_records'])
        db.rollback()
    print(json.dumps(counts,indent=2))


if __name__=='__main__':
    main()
