from __future__ import annotations

import sqlite3
import unittest

from crypto_super_radar.challenge_01_ai_mentor import review_baseline
from crypto_super_radar.learning_experience_memory import LearningExperienceMemory


class Challenge01AIMentorTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.memory = LearningExperienceMemory(self.db)
        with self.db:
            self.memory.record_decision(
                experience_id="baseline-1", event_id="decision-1",
                engine_id="CH01_SINGLE_CANDLE", challenge_id="CHALLENGE-01",
                cycle_id="cycle-1", decision_mode="BOT_ONLY",
                environment="BLOFIN_DEMO", market="SPOT", symbol="BTC-USDT",
                timeframe="1H", model_version="ch01-rule-v1",
                dataset_version="round-1",
                decision={
                    "candle_classification": "BULLISH_CANDLE",
                    "confidence_scope": "DETERMINISTIC_CLOSED_CANDLE_CLASSIFICATION_NOT_MARKET_FORECAST",
                },
                input_snapshot={"data_quality": "VALID_COMPLETED_CANDLE"},
            )
            self.memory.record_outcome(
                experience_id="baseline-1", event_id="outcome-1",
                decision_correctness="CORRECT", realized_result="NOT_EXECUTED",
                realized_net_pnl=None, final=True,
                evidence={
                    "source": "challenge_01_ohlc_rule_reference",
                    "comparison": "DETERMINISTIC_OHLC_LABEL_CONSISTENCY_NOT_PREDICTIVE_GROUND_TRUTH",
                },
            )

    def tearDown(self):
        self.db.close()

    def test_mentor_records_audit_without_mutating_baseline_or_awarding_mastery(self):
        result = review_baseline(self.memory, "baseline-1")
        self.assertEqual(result["status"], "MENTOR_REVIEW_RECORDED")
        self.assertFalse(result["mastery_awarded"])
        self.assertFalse(result["bot_only_decision_changed"])
        self.assertFalse(result["orders_submitted"])
        saved = self.memory.get_experience("baseline-1")
        self.assertEqual(saved["decision_mode"], "BOT_ONLY")
        self.assertEqual(
            [event["event_type"] for event in saved["events"]],
            ["DECISION_RECORDED", "OUTCOME_LABELLED", "MENTOR_FEEDBACK_RECORDED"],
        )
        feedback = saved["events"][-1]["payload"]["feedback"]
        self.assertIn("not a forecast", feedback)

    def test_mentor_is_idempotent_for_same_baseline(self):
        first = review_baseline(self.memory, "baseline-1")
        second = review_baseline(self.memory, "baseline-1")
        self.assertEqual(first["feedback_event_id"], second["feedback_event_id"])
        self.assertEqual(self.memory.get_experience("baseline-1")["verification"]["status"], "PASS")

    def test_mentor_rejects_other_challenge(self):
        with self.assertRaises(ValueError):
            review_baseline(self.memory, "missing-baseline")


if __name__ == "__main__":
    unittest.main()
