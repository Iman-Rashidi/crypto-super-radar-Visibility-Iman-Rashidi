"""Unit tests for post-trade autopsy and prompt-safe history."""
import unittest
from crypto_super_radar.learning_experience_memory import classify_trade_autopsy, build_historical_context_for_prompt


class TradeAutopsyTests(unittest.TestCase):
    def test_trailing_stop_missing(self):
        result = classify_trade_autopsy({"side": "LONG"}, {"trailing_stop_present": False},
            {"realized_result": "LOSS", "realized_net_pnl": -2}, 3.2, -2.4)
        self.assertEqual(result["diagnosis_code"], "TRAILING_STOP_MISSING")
        self.assertIn("حد سود متحرک", result["explanation_fa"])

    def test_late_wave(self):
        result = classify_trade_autopsy({"side": "LONG", "wave_progress_pct": 91}, {},
            {"realized_result": "LOSS", "realized_net_pnl": -1}, .2, -1.1)
        self.assertEqual(result["diagnosis_code"], "LATE_WAVE_ENTRY")

    def test_higher_timeframe_conflict(self):
        result = classify_trade_autopsy({"side": "LONG", "trend_4h": "BEARISH"}, {},
            {"realized_result": "LOSS", "realized_net_pnl": -1})
        self.assertEqual(result["diagnosis_code"], "HIGHER_TF_CONFLICT")

    def test_context_filters_unverified_and_free_text(self):
        exps = []
        for result, code in (("PROFIT", "LATE_WAVE_ENTRY"), ("LOSS", "LATE_WAVE_ENTRY"), ("PROFIT", "untrusted")):
            exps.append({"verification": {"status": "PASS"}, "events": [
                {"event_type": "DECISION_RECORDED", "payload": {"symbol": "BTC-USDT"}},
                {"event_type": "OUTCOME_LABELLED", "payload": {"final": True,
                 "realized_result": result, "realized_net_pnl": 1 if result == "PROFIT" else -1,
                 "evidence": {"post_trade_autopsy": {"diagnosis_code": code}}}}]})
        exps.append({"verification": {"status": "FAIL"}, "events": exps[0]["events"]})
        context = build_historical_context_for_prompt(exps, "BTC-USDT", 20)
        self.assertIn("VERIFIED_FINAL_SAMPLES=3", context)
        self.assertIn("WIN_RATE_PCT=66.7", context)
        self.assertIn("TOP_DIAGNOSIS=LATE_WAVE_ENTRY", context)
        self.assertNotIn("untrusted", context)


if __name__ == "__main__":
    unittest.main()
