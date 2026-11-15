from __future__ import annotations

import unittest
from types import SimpleNamespace

from crypto_super_radar.engine_profiles import (
    AGGRESSIVE, STRICT, evaluate_demo_candidate,
)


class EngineProfileTests(unittest.TestCase):
    def base(self):
        return dict(
            gates=[SimpleNamespace(gate_id=i, status="PASS") for i in range(1, 13)],
            current_open_risk_pct=0.0, data_valid=True, account_valid=True,
            instrument_supported=True, protective_orders_ready=True,
            stop_target_valid=True, nemotron_status="APPROVED",
        )

    def test_profiles_have_separate_risk_budgets_and_no_order_authority(self):
        self.assertEqual(AGGRESSIVE.strategy_risk_pct, 0.01)
        self.assertEqual(STRICT.strategy_risk_pct, 0.005)
        result = evaluate_demo_candidate(AGGRESSIVE.engine_id, **self.base())
        self.assertTrue(result["eligible"])
        self.assertFalse(result["execution_authorized"])

    def test_aggressive_skips_only_strategy_gate_screen(self):
        args = self.base()
        args["gates"] = []
        result = evaluate_demo_candidate(AGGRESSIVE.engine_id, **args)
        self.assertTrue(result["eligible"])
        self.assertEqual(result["strategy_gate_mode"], "SKIPPED_BY_AGGRESSIVE_PROFILE")

    def test_strict_requires_exactly_twelve_unique_passes(self):
        args = self.base()
        args["gates"] = args["gates"][:-1] + [SimpleNamespace(gate_id=12, status="UNKNOWN")]
        result = evaluate_demo_candidate(STRICT.engine_id, **args)
        self.assertFalse(result["eligible"])
        self.assertIn("STRICT_12_GATE_SCREEN_FAILED", result["blockers"])

    def test_universal_safety_guards_apply_to_both_engines(self):
        for profile in (AGGRESSIVE, STRICT):
            args = self.base()
            args["protective_orders_ready"] = False
            result = evaluate_demo_candidate(profile.engine_id, **args)
            self.assertFalse(result["eligible"])
            self.assertIn("EXCHANGE_PROTECTIVE_ORDERS_NOT_READY", result["blockers"])

    def test_shared_portfolio_cap_blocks_new_risk(self):
        args = self.base()
        args["current_open_risk_pct"] = 0.019
        result = evaluate_demo_candidate(AGGRESSIVE.engine_id, **args)
        self.assertTrue(result["eligible"])
        self.assertEqual(result["available_risk_budget_pct"], 0.001)
        args["current_open_risk_pct"] = 0.020
        self.assertFalse(evaluate_demo_candidate(AGGRESSIVE.engine_id, **args)["eligible"])

    def test_stop_bot_and_duplicate_signal_block(self):
        args = self.base()
        args["stop_bot"] = True
        self.assertFalse(evaluate_demo_candidate(AGGRESSIVE.engine_id, **args)["eligible"])
        args = self.base()
        args["duplicate_signal"] = True
        self.assertFalse(evaluate_demo_candidate(AGGRESSIVE.engine_id, **args)["eligible"])


if __name__ == "__main__":
    unittest.main()
