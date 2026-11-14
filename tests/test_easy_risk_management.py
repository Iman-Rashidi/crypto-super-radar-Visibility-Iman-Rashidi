from __future__ import annotations

import unittest
import numpy as np

from crypto_super_radar.easy_risk_management import (
    calculate_position_size, assess_leverage_margin, classify_market_cycle,
    STRATEGY_STYLE_GUIDE, calculate_fractional_kelly,
    historical_var_expected_shortfall, stress_test_positions,
    portfolio_risk_summary, portfolio_correlation_analysis, estimate_beta_hedge,
)


class EasyRiskManagementTests(unittest.TestCase):
    def test_fixed_percent_position_size(self):
        result = calculate_position_size(10000, 50, 48, risk_pct=2)
        self.assertEqual(result["status"], "KNOWN")
        self.assertLessEqual(result["modeled_loss_including_estimated_costs"], 200)
        self.assertAlmostEqual(result["quantity"], 100, delta=1)

    def test_fixed_amount_risk_and_notional_cap(self):
        result = calculate_position_size(10000, 50, 48, fixed_risk_amount=100, max_notional_pct=5)
        self.assertEqual(result["method"], "FIXED_RISK_AMOUNT")
        self.assertLessEqual(result["position_notional"], 500)

    def test_atr_distance_is_not_confused_with_position_notional(self):
        result = calculate_position_size(10000, 50, 49, risk_pct=1, atr_value=1, atr_multiple=2)
        self.assertEqual(result["stop_distance"], 2)
        self.assertLess(result["position_notional"], 10000)
        self.assertGreater(result["quantity"], 0)

    def test_invalid_prices_are_unknown(self):
        self.assertEqual(calculate_position_size(10000, 0, 1)["status"], "UNKNOWN")

    def test_leverage_margin_does_not_invent_liquidation_price(self):
        result = assess_leverage_margin(10000, 1000, 10, maintenance_margin_rate=0.005)
        self.assertEqual(result["status"], "KNOWN")
        self.assertIsNone(result["liquidation_price"])
        self.assertEqual(result["liquidation_status"], "UNKNOWN")
        self.assertEqual(result["initial_margin_estimate"], 1000)

    def test_market_cycle_missing_data_is_unknown(self):
        import pandas as pd
        self.assertEqual(classify_market_cycle(pd.DataFrame())["status"], "UNKNOWN")

    def test_market_cycle_detects_directional_phase_as_heuristic(self):
        import pandas as pd
        n = 60
        close = np.linspace(100, 120, n)
        frame = pd.DataFrame({"high": close * 1.01, "low": close * 0.99,
                              "close": close, "volume": np.full(n, 1000.0)})
        result = classify_market_cycle(frame)
        self.assertEqual(result["phase"], "MARK_UP")
        self.assertTrue(result["heuristic"])
        self.assertFalse(result["used_as_hard_gate"])

    def test_strategy_styles_are_separate_metadata(self):
        self.assertEqual(set(STRATEGY_STYLE_GUIDE), {"SCALPING", "DAY_TRADING", "SWING", "POSITION"})

    def test_kelly_requires_minimum_sample_and_caps_fraction(self):
        self.assertEqual(calculate_fractional_kelly(.55, 1.5, sample_size=10)["status"], "UNKNOWN")
        result = calculate_fractional_kelly(.60, 1.5, sample_size=100, max_bankroll_fraction=.02)
        self.assertEqual(result["status"], "KNOWN")
        self.assertLessEqual(result["applied_bankroll_fraction"], .02)
        self.assertFalse(result["used_for_default_position_sizing"])

    def test_historical_var_and_expected_shortfall(self):
        returns = np.linspace(-.20, .15, 100)
        result = historical_var_expected_shortfall(returns, confidence=.95)
        self.assertEqual(result["status"], "KNOWN")
        self.assertGreater(result["expected_shortfall_loss_pct"], 0)
        self.assertGreaterEqual(result["expected_shortfall_loss_pct"], result["var_loss_pct"])

    def test_var_requires_sufficient_history(self):
        self.assertEqual(historical_var_expected_shortfall([-.1, .1])["status"], "UNKNOWN")

    def test_stress_test_long_and_short(self):
        result = stress_test_positions([
            {"notional": 1000, "side": "LONG"},
            {"notional": 500, "side": "SHORT"},
        ], {"DOWN_10": -.10})
        self.assertEqual(result["status"], "KNOWN")
        self.assertAlmostEqual(result["scenarios"]["DOWN_10"]["pnl_estimate"], -50)

    def test_portfolio_risk_uses_stop_distance(self):
        result = portfolio_risk_summary([
            {"quantity": 10, "entry": 100, "stop": 95, "side": "LONG"}
        ], equity=10000)
        self.assertEqual(result["status"], "KNOWN")
        self.assertEqual(result["stop_risk"], 50)
        self.assertAlmostEqual(result["open_stop_risk_pct_equity"], .5)

    def test_portfolio_correlation_requires_common_history(self):
        result = portfolio_correlation_analysis(
            {"BTC": np.linspace(-.02, .02, 60), "ETH": np.linspace(-.01, .03, 60)},
            {"BTC": 1000, "ETH": 500},
        )
        self.assertEqual(result["status"], "KNOWN")
        self.assertGreaterEqual(result["highest_pairwise_correlation"], .9)

    def test_hedge_beta_returns_opposite_position_size(self):
        hedge = np.linspace(-.03, .03, 80)
        asset = 1.5 * hedge
        result = estimate_beta_hedge(asset, hedge, 10000, 100, hedge_ratio=.5)
        self.assertEqual(result["status"], "KNOWN")
        self.assertAlmostEqual(result["beta"], 1.5, places=5)
        self.assertEqual(result["hedge_side"], "SHORT")
        self.assertAlmostEqual(result["hedge_notional"], 7500, places=2)


if __name__ == "__main__":
    unittest.main()
