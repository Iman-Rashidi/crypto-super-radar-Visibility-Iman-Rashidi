from __future__ import annotations

import unittest

from crypto_super_radar.blofin_demo_executor import (
    BloFinDemoExecutionBlocked,
    BloFinDemoRiskPolicy,
    build_demo_order_plan,
)


def instrument():
    return {
        "instId": "BTC-USDT", "apiPermission": "full", "state": "live",
        "contractType": "linear", "settleCurrency": "USDT",
        "contractValue": "0.001", "minSize": "0.1",
        "lotSize": "0.1", "tickSize": "0.1", "maxMarketSize": "100000",
    }


def record():
    return {
        "signal": "LONG", "signal_id": "challenge1-closed-bar-001",
        "inst_id": "BTC-USDT", "execution_profile": "CHALLENGE_1",
        "entry_price_usd": 100.0, "stop_loss_usd": 98.0,
        "take_profit_usd": 104.0,
        "confirmation_score": None, "estimated_risk_score": None, "gates": [],
        "challenge1_evidence": {
            "setup_timeframe": "15m", "context_timeframe": "1H",
            "completed_candles_only": True, "legacy_12_gates_used_for_entry": False,
            "external_ai_required": False, "tinyfish_required": False,
            "decision": "LONG", "decision_engine_version": "CHALLENGE_1_LOCAL_RULES_V1",
            "detected_patterns": [{"name_en": "Bullish Engulfing", "direction": "BULLISH"}],
            "chosen_pattern": {"name_en": "Bullish Engulfing", "direction": "BULLISH"},
            "confirmation_candle": {
                "open_time_utc": "2026-11-16T10:00:00+00:00",
                "volume_confirmation": {"confirmed": True, "volume_ratio": 1.4},
            },
        },
    }


class Challenge1OrderPlanTests(unittest.TestCase):
    def test_pattern_profile_does_not_require_twelve_gate_rows_or_external_ai(self):
        plan = build_demo_order_plan(
            record(), instrument(), inst_id="BTC-USDT",
            equity_usdt=1000, leverage=1,
            policy=BloFinDemoRiskPolicy(max_leverage=1),
        )
        self.assertEqual(plan["side"], "LONG")
        self.assertEqual(plan["leverage"], 1)
        self.assertEqual(plan["risk_budget_usdt"], 2.5)

    def test_pattern_profile_rejects_missing_closed_candle_evidence(self):
        bad = record()
        bad["challenge1_evidence"]["completed_candles_only"] = False
        with self.assertRaises(BloFinDemoExecutionBlocked):
            build_demo_order_plan(
                bad, instrument(), inst_id="BTC-USDT", equity_usdt=1000,
                leverage=1, policy=BloFinDemoRiskPolicy(max_leverage=1),
            )

    def test_legacy_execution_profiles_are_rejected(self):
        for legacy_profile in ("EASY", "STANDARD", "STRICT"):
            bad = record()
            bad["execution_profile"] = legacy_profile
            with self.subTest(profile=legacy_profile), self.assertRaises(BloFinDemoExecutionBlocked):
                build_demo_order_plan(
                    bad, instrument(), inst_id="BTC-USDT", equity_usdt=1000,
                    leverage=1, policy=BloFinDemoRiskPolicy(max_leverage=1),
                )

    def test_pattern_profile_rejects_pattern_direction_mismatch(self):
        bad = record()
        bad["challenge1_evidence"]["chosen_pattern"]["direction"] = "BEARISH"
        with self.assertRaises(BloFinDemoExecutionBlocked):
            build_demo_order_plan(
                bad, instrument(), inst_id="BTC-USDT", equity_usdt=1000,
                leverage=1, policy=BloFinDemoRiskPolicy(max_leverage=1),
            )


if __name__ == "__main__":
    unittest.main()
