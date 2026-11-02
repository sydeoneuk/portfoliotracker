"""Read-only comparison of legacy and verified account price selection. No network."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import text
from app.database import SessionLocal
from app.valuation import effective_price_sql


def compare(db, user_id=None):
    query = f'''SELECT p.ticker, p.platform_id, p.user_id, p.quantity,
        {effective_price_sql(use_account_prices=False)} AS legacy_price,
        {effective_price_sql()} AS account_price
        FROM positions p WHERE p.quantity>0'''
    params = {}
    if user_id is not None:
        query += ' AND p.user_id=:u'
        params['u'] = user_id
    counts = dict(holdings=0, unchanged=0, changed=0, newly_priced=0, newly_unavailable=0, still_unavailable=0)
    for row in db.execute(text(query), params).mappings():
        old, new = row['legacy_price'], row['account_price']
        counts['holdings'] += 1
        if old is None and new is None:
            key = 'still_unavailable'
        elif old is None:
            key = 'newly_priced'
        elif new is None:
            key = 'newly_unavailable'
        else:
            key = 'unchanged' if abs(float(new) - float(old)) <= 1e-8 else 'changed'
        counts[key] += 1
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--user-id', type=int)
    args = parser.parse_args()
    with SessionLocal() as db:
        db.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
        result = compare(db, args.user_id)
        db.rollback()
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
