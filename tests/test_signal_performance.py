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
        self.assertEqual(report["slippage_attribution"], "NOT_SEPARATELY_RECORDED; any modeled slippage embedded in fill prices cannot be isolated here.")

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

    def test_invalid_configuration_is_rejected(self):
        with self.assertRaises(ValueError):
            summarize_closed_trades([], initial_equity_usd=0)
        with self.assertRaises(ValueError):
            summarize_closed_trades([], min_sample_size=0)


if __name__ == "__main__":
    unittest.main()
