from types import SimpleNamespace
from app.import_preview import import_effect


def batch(source, **kwargs):
    return SimpleNamespace(source=source, history_complete=False, status='review', review={}, **kwargs)


def test_partial_history_does_not_claim_to_rebuild_holdings_or_measure_upload_costs():
    for source in ('t212_history', 'ii_transactions'):
        item = batch(source)
        item.review = {'proposed': [{'quantity': 10}]}
        result = import_effect(item)
        assert 'stay unchanged' in result['explanation']
        assert 'costs_unavailable' not in result


def test_statement_counts_unknown_costs_but_keeps_explicit_zero_cost():
    item = batch('ii_holdings')
    item.review = {'proposed': [
        {'quantity': 10, 'cost_basis_status': 'reported', 'cost_basis_amount': '0', 'cost_basis_currency': 'GBP'},
        {'quantity': 2, 'cost_basis_status': 'unknown', 'cost_basis_amount': '20', 'cost_basis_currency': 'GBP'},
        {'quantity': 3, 'cost_basis_status': 'reported', 'cost_basis_amount': '10', 'cost_basis_currency': None},
        {'quantity': 0},
    ]}
    result = import_effect(item)
    assert result['holdings_count'] == 3
    assert result['costs_unavailable'] == 2
    item.status = 'rejected'
    assert 'holdings_count' not in import_effect(item)


def test_pie_and_complete_history_explain_different_effects():
    assert 'allocation only' in import_effect(batch('pie_snapshot'))['explanation']
    item = batch('t212_history')
    item.history_complete = True
    assert 'Rebuilds holdings' in import_effect(item)['explanation']
