from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from crypto_super_radar.challenge_01_ai_mentor import (
    review_baseline,
    review_locked_baseline,
)
from crypto_super_radar.learning_experience_memory import LearningExperienceMemory
from crypto_super_radar.challenge_01_runner import get_challenge_01_status


class Challenge01AIMentorTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tempdir.name) / "mentor-test.sqlite3")
        self.db = self.new_connection()
        self.memory = LearningExperienceMemory(self.db)

    def new_connection(self):
        db = sqlite3.connect(self.db_path)
        db.row_factory = sqlite3.Row
        return db

    def tearDown(self):
        self.db.close()
        self.tempdir.cleanup()

    def add_baseline(self, index: int, challenge_id: str = "CHALLENGE-01"):
        experience_id = f"baseline-{index}"
        with self.db:
            self.memory.record_decision(
                experience_id=experience_id, event_id=f"decision-{index}",
                engine_id="CH01_SINGLE_CANDLE", challenge_id=challenge_id,
                cycle_id=f"cycle-{index}", decision_mode="BOT_ONLY",
                environment="BLOFIN_DEMO", market="SPOT", symbol="BTC-USDT",
                timeframe="1H", model_version="ch01-rule-v1",
                dataset_version="round-1",
                decision={
                    "candle_classification": "BULLISH_CANDLE",
                    "confidence_scope": "DETERMINISTIC_CLOSED_CANDLE_CLASSIFICATION_NOT_MARKET_FORECAST",
                },
                input_snapshot={
                    "round_number": 1,
                    "candle_open_time_ms": 1_800_000_000_000 + index * 3_600_000,
                    "data_quality": "VALID_COMPLETED_CANDLE",
                },
            )
            self.memory.record_outcome(
                experience_id=experience_id, event_id=f"outcome-{index}",
                decision_correctness="CORRECT", realized_result="NOT_EXECUTED",
                realized_net_pnl=None, final=True,
                evidence={
                    "source": "challenge_01_ohlc_rule_reference",
                    "comparison": "DETERMINISTIC_OHLC_LABEL_CONSISTENCY_NOT_PREDICTIVE_GROUND_TRUTH",
                },
            )
        return experience_id

    def test_mentor_records_audit_without_mutating_baseline_or_awarding_mastery(self):
        self.add_baseline(1)
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
        self.add_baseline(1)
        first = review_baseline(self.memory, "baseline-1")
        second = review_baseline(self.memory, "baseline-1")
        self.assertEqual(first["feedback_event_id"], second["feedback_event_id"])
        self.assertEqual(self.memory.get_experience("baseline-1")["verification"]["status"], "PASS")

    def test_mentor_rejects_other_challenge(self):
        self.add_baseline(1, challenge_id="CHALLENGE-02")
        with self.assertRaises(ValueError):
            review_baseline(self.memory, "baseline-1")

    def test_batch_review_requires_at_least_100_locked_samples(self):
        self.add_baseline(1)
        result = review_locked_baseline(
            connection_factory=self.new_connection,
            backend_factory=lambda: "TEST_SQLITE_FILE",
            allow_non_durable_for_tests=True,
        )
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["reason_code"], "BOT_ONLY_BASELINE_INCOMPLETE")
        saved = self.memory.get_experience("baseline-1")
        self.assertNotIn("MENTOR_FEEDBACK_RECORDED", [e["event_type"] for e in saved["events"]])

    def test_batch_review_requires_durable_backend_outside_tests(self):
        result = review_locked_baseline(
            connection_factory=lambda: self.db,
            backend_factory=lambda: "LOCAL_SQLITE",
        )
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["reason_code"], "DURABLE_LEARNING_MEMORY_REQUIRED")
        self.assertFalse(result["orders_submitted"])

    def test_batch_review_records_and_verifies_all_100_baseline_experiences(self):
        for index in range(100):
            self.add_baseline(index)
        result = review_locked_baseline(
            connection_factory=self.new_connection,
            backend_factory=lambda: "TEST_SQLITE_FILE",
            allow_non_durable_for_tests=True,
        )
        self.assertEqual(result["status"], "MENTOR_REVIEW_RECORDED")
        self.assertEqual(result["baseline_samples"], 100)
        self.assertEqual(result["mentor_reviews_recorded"], 100)
        self.assertEqual(result["next_stage"], "ASSISTED_ROUND_RUNNER_REQUIRED")
        self.assertFalse(result["mastery_awarded"])
        self.assertFalse(result["orders_submitted"])
        self.assertTrue(all(
            self.memory.get_experience(f"baseline-{index}")["verification"]["status"] == "PASS"
            for index in range(100)
        ))
        retry = review_locked_baseline(
            connection_factory=self.new_connection,
            backend_factory=lambda: "TEST_SQLITE_FILE",
            allow_non_durable_for_tests=True,
        )
        self.assertEqual(retry["status"], "MENTOR_REVIEW_RECORDED")
        self.assertEqual(retry["mentor_reviews_recorded"], 100)
        status = get_challenge_01_status(
            connection_factory=self.new_connection,
            backend_factory=lambda: "TEST_SQLITE_FILE",
            allow_non_durable_for_tests=True,
        )
        self.assertTrue(status["mentor_review_complete"])
        self.assertEqual(status["mentor_reviewed_samples"], 100)
        self.assertEqual(status["next_stage"], "ASSISTED_ROUND_RUNNER_REQUIRED")
        self.assertTrue(status["later_rounds_blocked"])


if __name__ == "__main__":
    unittest.main()
