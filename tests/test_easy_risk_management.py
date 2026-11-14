from __future__ import annotations

import unittest
import numpy as np
import pandas as pd

from crypto_super_radar.easy_risk_management import (
    calculate_position_size,
    assess_leverage_margin,
    classify_market_cycle,
    STRATEGY_STYLE_GUIDE,
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
        self.assertEqual(classify_market_cycle(pd.DataFrame())["status"], "UNKNOWN")

    def test_market_cycle_detects_directional_phase_as_heuristic(self):
        n = 60
        close = np.linspace(100, 120, n)
        frame = pd.DataFrame({
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": np.full(n, 1000.0),
        })
        result = classify_market_cycle(frame)
        self.assertEqual(result["phase"], "MARK_UP")
        self.assertTrue(result["heuristic"])
        self.assertFalse(result["used_as_hard_gate"])

    def test_strategy_styles_are_separate_metadata(self):
        self.assertEqual(set(STRATEGY_STYLE_GUIDE), {"SCALPING", "DAY_TRADING", "SWING", "POSITION"})


if __name__ == "__main__":
    unittest.main()
