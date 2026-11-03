import datetime as dt
import unittest
from app.portfolio_history import build_history


def record(day, account, value, pie=None):
    return dict(snapshot_date=dt.date(2026, 11, day), platform_id=account,
                scope_type='account' if pie is None else 'pie', pie_id=pie,
                total_value_gbp=value)


class TestPortfolioHistory(unittest.TestCase):
    def test_independent_account_dates_do_not_make_total_drop(self):
        data = build_history([record(1, 1, 100), record(1, 2, 200), record(2, 1, 110)], [1, 2], current_value=320)
        self.assertEqual(data['values'], [300, 310, 320])
        self.assertEqual(data['iso_dates'], ['2026-11-01', None, None])
        self.assertEqual(data['labels'][-1], 'Current')
        self.assertIn('last recorded', data['note'])

    def test_unknown_opening_values_show_available_accounts_without_backfilling(self):
        data = build_history([record(1, 1, 100), record(2, 2, 200)], [1, 2])
        self.assertEqual(data['values'], [100, 300])
        self.assertIn('partial totals', data['note'])
        self.assertIn('1 of 2 accounts', data['point_notes'][0])
        self.assertIn('carried forward', data['point_notes'][1])

    def test_partial_total_discloses_both_missing_accounts_and_carried_values(self):
        data = build_history([record(1, 1, 100), record(2, 2, 200)], [1, 2, 3])
        self.assertEqual(data['values'], [100, 300])
        self.assertIn('2 of 3 accounts', data['point_notes'][1])
        self.assertIn('carried forward', data['point_notes'][1])

    def test_pie_only_date_with_no_recorded_account_value_is_not_zero(self):
        data = build_history([record(1, 1, 20, pie=10)], [1, 2])
        self.assertEqual(data['values'], [None])

    def test_outside_pie_subtracts_from_each_account_instead_of_global_max(self):
        rows = [record(1, 1, 100), record(1, 2, 200), record(1, 1, 20, 10), record(1, 2, 30, 20)]
        self.assertEqual(build_history(rows, [1, 2], include_no_pie=True)['values'], [250])
        self.assertEqual(build_history(rows, [1, 2], [10], True)['values'], [270])
        self.assertEqual(build_history(rows, [1], [10])['values'], [20])

    def test_zero_account_snapshot_replaces_carried_balance(self):
        rows = [record(1, 1, 100), record(1, 2, 200), record(2, 1, 0)]
        self.assertEqual(build_history(rows, [1, 2])['values'], [300, 200])

    def test_live_value_already_in_display_currency_is_not_converted_twice(self):
        data = build_history([record(1, 1, 100)], [1], convert=lambda v: v * 1.25, current_value=150, currency='USD')
        self.assertEqual(data['values'], [125, 150])
        self.assertEqual(data['currency'], 'USD')

    def test_missing_current_quote_never_adds_a_partial_live_total(self):
        data = build_history([record(1, 1, 100)], [1], current_value=None)
        self.assertEqual(data['labels'], ['01 Nov 2026'])
        self.assertIn('Current total unavailable', data['note'])

    def test_historical_view_does_not_append_current_or_future_values(self):
        data = build_history([record(1, 1, 100), record(2, 1, 120)], [1], current_value=999, as_of_date=dt.date(2026, 11, 1))
        self.assertEqual(data['values'], [100])

    def test_country_sector_filter_does_not_mix_unfiltered_history_with_filtered_card(self):
        data = build_history([record(1, 1, 100)], [1], current_value=20, filtered=True)
        self.assertEqual(data['values'], [20])
        self.assertIn('country or sector', data['note'])

    def test_missing_fx_keeps_history_unavailable_without_failing_page(self):
        def unavailable(value):
            raise ValueError('Exchange rate unavailable')
        data = build_history([record(1, 1, 100)], [1], convert=unavailable, currency='USD')
        self.assertEqual(data['values'], [None])
