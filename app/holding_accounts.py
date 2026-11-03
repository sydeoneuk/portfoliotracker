"""Account badges describe the accounts contributing to each displayed holding."""
from app.models import Platform

PROVIDER_CODES = {
    't212_api': 'T212', 't212_csv': 'T212', 'interactive_investor': 'II',
    'charles_stanley': 'CS', 'best_invest': 'BI',
}


def decorate_holding_accounts(db, user_id, rows):
    ids = {pid for row in rows for pid in row.get('platform_ids') or [] if pid is not None}
    platforms = {p.id: p for p in db.query(Platform).filter(
        Platform.user_id == user_id, Platform.id.in_(ids)).all()} if ids else {}
    for row in rows:
        badges = []
        for pid in set(row.get('platform_ids') or []):
            platform = platforms.get(pid)
            if platform is None:
                continue
            code = PROVIDER_CODES.get(platform.platform_type, platform.name)
            wrapper = 'GIA' if platform.account_type == 'Trading' else platform.account_type
            badges.append(dict(id=pid, label=f'{code}-{wrapper}', name=platform.name))
        row['holding_accounts'] = sorted(badges, key=lambda badge: (badge['label'], badge['name'], badge['id']))
    return rows
