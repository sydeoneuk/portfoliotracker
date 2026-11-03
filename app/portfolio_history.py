"""Read-only chart projection; recorded account history is never rewritten."""
import math
from collections import defaultdict


def build_history(rows, accounts, pie_ids=(), include_no_pie=False, *, convert=lambda v: v,
                  current_value=None, currency='GBP', as_of_date=None, filtered=False):
    result = dict(labels=[], iso_dates=[], values=[], latest=None, currency=currency,
                  point_notes=[], note='')
    accounts = set(accounts)
    days = defaultdict(dict)
    if not filtered:
        for row in rows:
            if row['platform_id'] not in accounts:
                continue
            if as_of_date and row['snapshot_date'] > as_of_date:
                continue
            # Query orders by capture time/id: retain the latest duplicate only.
            days[row['snapshot_date']][(row['platform_id'], row['scope_type'], row['pie_id'])] = row
    state = {}
    carried_any = missing_any = False
    conversion_missing = False
    for day, observations in sorted(days.items()):
        for pid in accounts:
            account = observations.get((pid, 'account', None))
            if account is None:
                continue
            pies = {key[2]: row['total_value_gbp'] for key, row in observations.items()
                    if key[0] == pid and key[1] == 'pie'}
            total = float(account['total_value_gbp'])
            if pie_ids or include_no_pie:
                selected = sum(float(pies.get(pie, 0)) for pie in pie_ids)
                total = total - sum(float(v) for v in pies.values()) + selected if include_no_pie else selected
            state[pid] = (day, total)
        missing = accounts - state.keys()
        carried = any(recorded < day for recorded, total in state.values())
        missing_any |= bool(missing)
        carried_any |= carried
        note = (f'Partial total: {len(state)} of {len(accounts)} accounts have recorded values' if missing
                else 'Recorded daily value')
        if carried:
            note += '; includes last recorded account values carried forward'
        try:
            value = round(convert(sum(total for recorded, total in state.values())), 2) if state else None
        except (ValueError, ArithmeticError):
            value = None
            conversion_missing = True
        result['labels'].append(day.strftime('%d %b %Y'))
        # A carried total has no single matching historical holdings snapshot.
        result['iso_dates'].append(None if missing or carried else day.isoformat())
        result['values'].append(value)
        result['point_notes'].append(note)
    if not as_of_date and current_value is not None and math.isfinite(float(current_value)):
        result['labels'].append('Current')
        result['iso_dates'].append(None)
        result['values'].append(round(float(current_value), 2))
        result['point_notes'].append('Current market value, matching the card above')
    result['latest'] = result['values'][-1] if result['values'] else None
    notes = []
    if filtered:
        notes.append('Historical totals for country or sector filters are unavailable; showing the current filtered value.')
    if carried_any:
        notes.append('Gaps use each account’s last recorded value; these are not fresh daily valuations.')
    if missing_any:
        notes.append('Earlier points may be partial totals: accounts without a recorded value are excluded.')
    if not as_of_date and current_value is None:
        notes.append('Current total unavailable because some holdings cannot be valued.')
    if currency != 'GBP':
        notes.append('Recorded GBP totals are converted using the current display exchange rate.')
    if conversion_missing:
        notes.append('Historical totals cannot be displayed while the exchange rate is unavailable.')
    result['note'] = ' '.join(notes)
    return result
