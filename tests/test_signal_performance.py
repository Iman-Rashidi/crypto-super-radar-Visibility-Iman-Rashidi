import unittest

from crypto_super_radar.signal_performance import summarize_closed_trades


def _trade(net, *, gross=None, stop=90.0):
    return {
        "net_pnl": net,
        "gross_pnl": net + 2.0 if gross is None else gross,
        "entry_fee": 1.0,
        "exit_fee": 1.0,
        "quantity": 1.0,
        "entry_price": 100.0,
        "initial_stop_loss": stop,
        "entry_slippage_usd": 0.2,
        "exit_slippage_usd": 0.3,
        "spread_cost_usd": 0.4,
        "funding_cost_usd": 0.0,
        "funding_status": "NOT_APPLICABLE",
    }


class SignalPerformanceTests(unittest.TestCase):
    def test_small_sample_is_not_presented_as_edge(self):
        report = summarize_closed_trades([_trade(5.0), _trade(-2.0)])
        self.assertEqual(report["status"], "INSUFFICIENT_SAMPLE")
        self.assertFalse(report["performance_claim_allowed"])
        self.assertEqual(report["sample_size"], 2)

    def test_cost_adjusted_metrics_and_r_multiple(self):
        trades = [_trade(5.0), _trade(-2.0), _trade(3.0)]
        report = summarize_closed_trades(trades, min_sample_size=3)
        self.assertEqual(report["status"], "REVIEWABLE")
        self.assertEqual(report["wins"], 2)
        self.assertEqual(report["losses"], 1)
        self.assertEqual(report["total_recorded_fees_usd"], 6.0)
        self.assertAlmostEqual(report["net_expectancy_usd_per_trade"], 2.0)
        self.assertAlmostEqual(report["net_expectancy_r"], 0.2)
        self.assertFalse(report["performance_claim_allowed"])
        self.assertEqual(report["slippage_attribution"], "RECORDED_PER_FILL")
        self.assertEqual(report["total_recorded_slippage_usd"], 1.5)
        self.assertEqual(report["total_recorded_spread_usd"], 1.2)
        self.assertEqual(report["execution_cost_attribution_status"], "COMPLETE")

    def test_missing_initial_stop_does_not_fabricate_r(self):
        trade = _trade(1.0)
        trade["initial_stop_loss"] = None
        report = summarize_closed_trades([trade], min_sample_size=1)
        self.assertEqual(report["r_sample_size"], 0)
        self.assertIsNone(report["net_expectancy_r"])

    def test_invalid_rows_are_excluded(self):
        report = summarize_closed_trades([_trade(1.0), {"net_pnl": float("nan"), "gross_pnl": 1.0}])
        self.assertEqual(report["sample_size"], 1)
        self.assertEqual(report["invalid_rows_excluded"], 1)

    def test_missing_fee_is_not_silently_counted_as_zero(self):
        trades = [_trade(5.0), _trade(-2.0)]
        trades[1]["entry_fee"] = None

        report = summarize_closed_trades(trades)

        self.assertEqual(report["sample_size"], 2)
        self.assertEqual(report["fee_data_status"], "INCOMPLETE")
        self.assertEqual(report["fee_incomplete_rows"], 1)
        self.assertEqual(report["fee_coverage_pct"], 50.0)
        self.assertEqual(report["known_recorded_fees_usd"], 3.0)
        self.assertIsNone(report["total_recorded_fees_usd"])
        self.assertIsNone(report["fees_as_pct_of_gross_profit"])
        self.assertFalse(report["performance_claim_allowed"])

    def test_explicit_zero_fees_are_valid_complete_observations(self):
        trade = _trade(1.0)
        trade["entry_fee"] = 0.0
        trade["exit_fee"] = 0.0

        report = summarize_closed_trades([trade], min_sample_size=1)

        self.assertEqual(report["fee_data_status"], "COMPLETE")
        self.assertEqual(report["fee_coverage_pct"], 100.0)
        self.assertEqual(report["total_recorded_fees_usd"], 0.0)

    def test_invalid_fee_component_is_unknown(self):
        trade = _trade(1.0)
        trade["exit_fee"] = float("nan")

        report = summarize_closed_trades([trade], min_sample_size=1)

        self.assertEqual(report["fee_data_status"], "INCOMPLETE")
        self.assertEqual(report["fee_incomplete_rows"], 1)
        self.assertIsNone(report["total_recorded_fees_usd"])


    def test_missing_spread_or_slippage_keeps_total_execution_cost_incomplete(self):
        trade = _trade(1.0)
        trade["spread_cost_usd"] = None
        trade["exit_slippage_usd"] = None
        report = summarize_closed_trades([trade], min_sample_size=1)
        self.assertEqual(report["slippage_data_status"], "INCOMPLETE")
        self.assertEqual(report["slippage_coverage_pct"], 0.0)
        self.assertIsNone(report["total_recorded_slippage_usd"])
        self.assertEqual(report["spread_data_status"], "INCOMPLETE")
        self.assertIsNone(report["total_recorded_spread_usd"])
        self.assertEqual(report["execution_cost_attribution_status"], "INCOMPLETE")
        self.assertFalse(report["performance_claim_allowed"])

    def test_invalid_configuration_is_rejected(self):
        with self.assertRaises(ValueError):
            summarize_closed_trades([], initial_equity_usd=0)
        with self.assertRaises(ValueError):
            summarize_closed_trades([], min_sample_size=0)


if __name__ == "__main__":
    unittest.main()
