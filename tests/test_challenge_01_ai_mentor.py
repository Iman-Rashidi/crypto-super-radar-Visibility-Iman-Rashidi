from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from crypto_super_radar.challenge_01_ai_mentor import (
    detect_candlestick_patterns,
    load_candidate_artifact,
    predict_candidate,
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

    def add_baseline(
        self, index: int, challenge_id: str = "CHALLENGE-01",
        include_target_in_input: bool = False,
    ):
        experience_id = f"baseline-{index}"
        feature_ts = 1_800_000_000_000 + index * 3_600_000
        target_ts = feature_ts + 3_600_000
        feature_window = [
            {
                "open_time_ms": feature_ts - 2 * 3_600_000,
                "open": "100", "high": "103", "low": "98", "close": "99",
                "confirmed": True,
            },
            {
                "open_time_ms": feature_ts - 3_600_000,
                "open": "99", "high": "102", "low": "97", "close": "100",
                "confirmed": True,
            },
            {
                "open_time_ms": feature_ts,
                "open": "100", "high": "105", "low": "95", "close": "101",
                "confirmed": True,
            },
        ]
        feature = feature_window[-1]
        target = {
            "open_time_ms": target_ts,
            "open": "101", "high": "106", "low": "96", "close": "102",
            "confirmed": True,
        }
        snapshot = {
            "round_number": 1,
            "candle_open_time_ms": target_ts,
            "feature_candle_open_time_ms": feature_ts,
            "target_candle_open_time_ms": target_ts,
            "feature_candle": feature,
            "feature_window": feature_window,
            "feature_window_open_times_ms": [candle["open_time_ms"] for candle in feature_window],
            "detected_candlestick_patterns": detect_candlestick_patterns(feature_window),
            "prediction_horizon_bars": 1,
            "data_quality": "VALID_COMPLETED_FEATURE_AND_TARGET",
            "target_candle_in_input": False,
        }
        if include_target_in_input:
            snapshot["target_candle"] = target
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
                    "action": "PREDICT_NEXT_BULLISH",
                    "confidence": None,
                    "rule": "previous_candle_body_direction",
                    "prediction_horizon_bars": 1,
                    "confidence_scope": "UNSCORED_BASELINE_RULE_NOT_CALIBRATED_PROBABILITY",
                },
                input_snapshot={
                    **snapshot,
                },
            )
            self.memory.record_outcome(
                experience_id=experience_id, event_id=f"outcome-{index}",
                decision_correctness="CORRECT", realized_result="NOT_EXECUTED",
                realized_net_pnl=None, final=True,
                evidence={
                    "source": "blofin_demo_spot_next_completed_candle",
                    "comparison": "NEXT_CANDLE_DIRECTION_FROM_NEXT_COMPLETED_BAR",
                    "feature_candle_open_time_ms": feature_ts,
                    "feature_window_open_times_ms": [candle["open_time_ms"] for candle in feature_window],
                    "target_candle_open_time_ms": target_ts,
                    "target_candle": target,
                    "prediction_horizon_bars": 1,
                    "no_future_data_used_in_input": not include_target_in_input,
                },
            )
        return experience_id

    def test_pattern_detector_recognizes_single_two_and_three_bar_examples(self):
        doji_window = [
            {"open_time_ms": i * 3_600_000, "open": "100", "high": "103", "low": "98", "close": "101", "confirmed": True}
            for i in range(3)
        ]
        doji_window[-1] = {
            "open_time_ms": 2 * 3_600_000, "open": "100", "high": "104",
            "low": "96", "close": "100", "confirmed": True,
        }
        self.assertIn("DOJI", detect_candlestick_patterns(doji_window))

        engulfing_window = [
            {"open_time_ms": 0, "open": "102", "high": "103", "low": "99", "close": "100", "confirmed": True},
            {"open_time_ms": 3_600_000, "open": "104", "high": "105", "low": "99", "close": "100", "confirmed": True},
            {"open_time_ms": 7_200_000, "open": "99", "high": "106", "low": "98", "close": "105", "confirmed": True},
        ]
        self.assertIn("BULLISH_ENGULFING", detect_candlestick_patterns(engulfing_window))

        morning_star_window = [
            {"open_time_ms": 0, "open": "110", "high": "111", "low": "99", "close": "100", "confirmed": True},
            {"open_time_ms": 3_600_000, "open": "100", "high": "101", "low": "99.5", "close": "100.2", "confirmed": True},
            {"open_time_ms": 7_200_000, "open": "100.5", "high": "109", "low": "100", "close": "108", "confirmed": True},
        ]
        self.assertIn("MORNING_STAR", detect_candlestick_patterns(morning_star_window))

    def test_pattern_detector_rejects_incomplete_or_wrong_length_windows(self):
        with self.assertRaises(ValueError):
            detect_candlestick_patterns([])
        incomplete = [
            {"open_time_ms": i * 3_600_000, "open": "100", "high": "102", "low": "98", "close": "101",
             "confirmed": i < 2}
            for i in range(3)
        ]
        with self.assertRaises(ValueError):
            detect_candlestick_patterns(incomplete)

    def test_mentor_records_audit_without_mutating_baseline_or_awarding_mastery(self):
        self.add_baseline(1)
        result = review_baseline(self.memory, "baseline-1")
        self.assertEqual(result["status"], "MENTOR_REVIEW_RECORDED")
        self.assertFalse(result["mastery_awarded"])
        self.assertFalse(result["bot_only_decision_changed"])
        self.assertFalse(result["orders_submitted"])
        self.assertEqual(result["issues"], [])
        saved = self.memory.get_experience("baseline-1")
        self.assertEqual(saved["decision_mode"], "BOT_ONLY")
        self.assertEqual(
            [event["event_type"] for event in saved["events"]],
            ["DECISION_RECORDED", "OUTCOME_LABELLED", "MENTOR_FEEDBACK_RECORDED"],
        )
        feedback = saved["events"][-1]["payload"]["feedback"]
        self.assertIn("naive one-bar momentum", feedback)
        self.assertIn("not a trained AI model", feedback)

    def test_mentor_flags_target_candle_leakage_in_decision_input(self):
        self.add_baseline(2, include_target_in_input=True)
        result = review_baseline(self.memory, "baseline-2")
        self.assertIn(
            "Future target data appears in, or is not explicitly excluded from, the decision input.",
            result["issues"],
        )
        saved = self.memory.get_experience("baseline-2")
        self.assertEqual(saved["decision_mode"], "BOT_ONLY")
        self.assertEqual(saved["events"][-1]["event_type"], "MENTOR_FEEDBACK_RECORDED")

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
        self.assertEqual(result["reason_code"], "BOT_ONLY_BASELINE_MUST_BE_EXACTLY_100_VALID_FORWARD_SAMPLES")
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
        self.assertEqual(result["status"], "MENTOR_REVIEW_RECORDED", msg=repr(result))
        self.assertEqual(result["baseline_samples"], 100)
        self.assertEqual(result["mentor_reviews_recorded"], 100)
        self.assertEqual(result["next_stage"], "FORWARD_HOLDOUT_VALIDATION_REQUIRED")
        candidate = result["model_candidate"]
        self.assertEqual(candidate["status"], "CANDIDATE_STORED_NOT_PROMOTED")
        self.assertEqual(candidate["training_samples"], 80)
        self.assertEqual(candidate["validation_samples"], 20)
        self.assertEqual(len(candidate["artifact_sha256"]), 64)
        self.assertEqual(candidate["promotion_status"], "CANDIDATE_NOT_PROMOTED")
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
        self.assertEqual(retry["model_candidate"]["artifact_sha256"], candidate["artifact_sha256"])
        status = get_challenge_01_status(
            connection_factory=self.new_connection,
            backend_factory=lambda: "TEST_SQLITE_FILE",
            allow_non_durable_for_tests=True,
        )
        self.assertTrue(status["mentor_review_complete"])
        self.assertEqual(status["mentor_reviewed_samples"], 100)
        self.assertEqual(status["next_stage"], "FORWARD_HOLDOUT_VALIDATION_REQUIRED")
        self.assertEqual(status["next_round"], 2)
        self.assertTrue(status["candidate_model_present"])
        self.assertEqual(status["candidate_model_sha256"], candidate["artifact_sha256"])
        self.assertFalse(status["later_rounds_blocked"])
        anchor = self.memory.get_experience("baseline-0")
        model_event = next(e for e in anchor["events"] if e["event_type"] == "LESSON_PROPOSED")
        stored_model = json.loads(model_event["payload"]["lesson"])
        model_hash = stored_model.pop("artifact_sha256")
        self.assertEqual(
            hashlib.sha256(json.dumps(stored_model, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest(),
            model_hash,
        )
        self.assertEqual(stored_model["training"]["samples"], 80)
        self.assertEqual(stored_model["temporal_validation"]["samples"], 20)
        self.assertEqual(stored_model["schema"], "CSR_CH01_CANDLE_PATTERN_SOFTMAX_V2")
        self.assertEqual(stored_model["feature_window_length"], 3)
        self.assertIn("bullish_engulfing_2bar", stored_model["features"])
        self.assertIn("morning_star_3bar", stored_model["features"])
        loaded_artifact, loaded_anchor = load_candidate_artifact(self.memory)
        self.assertEqual(loaded_artifact["artifact_sha256"], candidate["artifact_sha256"])
        self.assertEqual(loaded_anchor, "baseline-0")
        feature_window = self.memory.get_experience("baseline-0")["events"][0]["payload"]["input_snapshot"]["feature_window"]
        predicted, probabilities = predict_candidate(feature_window, loaded_artifact)
        self.assertIn(predicted, {"BULLISH_CANDLE", "BEARISH_CANDLE", "NEUTRAL_CANDLE"})
        self.assertAlmostEqual(sum(probabilities), 1.0, places=7)



if __name__ == "__main__":
    unittest.main()
