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
    signal_bar = "2026-11-16T10:00:00+00:00"
    candles_5m = [
        {
            "open_time_utc": "2026-11-16T09:50:00+00:00",
            "open": 98.5, "high": 99.5, "low": 98.0, "close": 99.0, "volume": 800.0,
        },
        {
            "open_time_utc": "2026-11-16T09:55:00+00:00",
            "open": 99.0, "high": 100.2, "low": 98.8, "close": 99.5, "volume": 900.0,
        },
        {
            "open_time_utc": signal_bar,
            "open": 99.5, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 1100.0,
        },
    ]
    signal_id = f"challenge1-v4:BTC-USDT:5m:{signal_bar}"
    pattern = {
        "name_en": "Bullish Engulfing", "direction": "BULLISH",
        "category": "CANDLESTICK", "timeframe": "5m",
    }
    return {
        "signal": "LONG", "signal_id": signal_id,
        "signal_bar_open_utc": signal_bar,
        "inst_id": "BTC-USDT", "instrument_id": "BTC-USDT",
        "execution_profile": "CHALLENGE_1",
        "execution": {"status": "PASS", "direction": "LONG", "signal_id": signal_id},
        "entry_price_usd": 100.0, "stop_loss_usd": 98.0,
        "take_profit_usd": 104.0,
        "confirmation_score": None, "estimated_risk_score": None, "gates": [],
        "challenge1_evidence": {
            "setup_timeframe": "5m", "completed_candles_only": True,
            "higher_timeframes_used": False, "legacy_12_gates_used_for_entry": False,
            "external_ai_required": False, "tinyfish_required": False,
            "decision": "LONG", "decision_engine_version": "CHALLENGE_1_LOCAL_RULES_V4_5M_ONLY",
            "detected_patterns": [pattern], "chosen_pattern": pattern,
            "confirmation_candle": dict(candles_5m[-1]), "candles_5m": candles_5m,
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
