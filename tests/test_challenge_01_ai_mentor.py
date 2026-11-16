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
    MODEL_FEATURES,
    _feature_vector,
    build_pattern_context,
    CONTEXT_WINDOW_LENGTH,
    MARKET_CONTEXT_VERSION,
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
                "open_time_ms": feature_ts - 4 * 3_600_000,
                "open": "100", "high": "103", "low": "98", "close": "100",
                "confirmed": True,
            },
            {
                "open_time_ms": feature_ts - 3 * 3_600_000,
                "open": "100", "high": "103", "low": "98", "close": "99",
                "confirmed": True,
            },
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
        for candle_item in feature_window:
            candle_item["volume"] = "10"
        context_prefix = [
            {
                "open_time_ms": feature_ts - (CONTEXT_WINDOW_LENGTH - index) * 3_600_000,
                "open": "100", "high": "103", "low": "98", "close": "100",
                "volume": "10", "confirmed": True,
            }
            for index in range(CONTEXT_WINDOW_LENGTH - FEATURE_WINDOW_LENGTH)
        ]
        context_window = context_prefix + feature_window
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
            "context_window": context_window,
            "context_window_open_times_ms": [candle["open_time_ms"] for candle in context_window],
            "detected_candlestick_patterns": detect_candlestick_patterns(feature_window),
            "pattern_context": build_pattern_context(context_window),
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
                    "reference_classification": "BULLISH_CANDLE",
                    "feature_candle_open_time_ms": feature_ts,
                    "feature_window_open_times_ms": [candle["open_time_ms"] for candle in feature_window],
                    "context_window_open_times_ms": [candle["open_time_ms"] for candle in context_window],
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
        neutral_prefix = [
            {"open_time_ms": -2 * 3_600_000, "open": "100", "high": "101", "low": "99", "close": "100", "confirmed": True},
            {"open_time_ms": -1 * 3_600_000, "open": "100", "high": "101", "low": "99", "close": "100", "confirmed": True},
        ]
        engulfing_vector = dict(zip(MODEL_FEATURES, _feature_vector(neutral_prefix + engulfing_window)))
        self.assertEqual(engulfing_vector["bullish_engulfing_2bar"], 1.0)

        morning_star_window = [
            {"open_time_ms": 0, "open": "110", "high": "111", "low": "99", "close": "100", "confirmed": True},
            {"open_time_ms": 3_600_000, "open": "100", "high": "101", "low": "99.5", "close": "100.2", "confirmed": True},
            {"open_time_ms": 7_200_000, "open": "100.5", "high": "109", "low": "100", "close": "108", "confirmed": True},
        ]
        self.assertIn("MORNING_STAR", detect_candlestick_patterns(morning_star_window))
        morning_vector = dict(zip(MODEL_FEATURES, _feature_vector(neutral_prefix + morning_star_window)))
        self.assertEqual(morning_vector["morning_star_3bar"], 1.0)

        weak_first_morning_window = [
            {"open_time_ms": 0, "open": "110", "high": "111", "low": "99", "close": "105", "confirmed": True},
            {"open_time_ms": 3_600_000, "open": "105", "high": "106", "low": "104.5", "close": "105.2", "confirmed": True},
            {"open_time_ms": 7_200_000, "open": "105.5", "high": "109", "low": "105", "close": "108", "confirmed": True},
        ]
        self.assertNotIn("MORNING_STAR", detect_candlestick_patterns(weak_first_morning_window))


    def test_extended_candle_pattern_catalog_and_five_bar_continuations(self):
        def bars(values):
            return [
                {
                    "open_time_ms": index * 3_600_000,
                    "open": str(opening), "high": str(high),
                    "low": str(low), "close": str(closing),
                    "confirmed": True,
                }
                for index, (opening, high, low, closing) in enumerate(values)
            ]

        neutral = (100, 101, 99, 100)
        self.assertIn("DRAGONFLY_DOJI", detect_candlestick_patterns(
            bars([neutral, neutral, neutral, neutral, (100, 101, 90, 100)])))
        self.assertIn("GRAVESTONE_DOJI", detect_candlestick_patterns(
            bars([neutral, neutral, neutral, neutral, (100, 110, 99, 100)])))
        self.assertIn("LONG_LEGGED_DOJI", detect_candlestick_patterns(
            bars([neutral, neutral, neutral, neutral, (100, 110, 90, 100)])))
        self.assertIn("BULLISH_KICKER", detect_candlestick_patterns(bars([
            neutral, neutral, neutral, (100, 101, 90, 92), (101, 110, 100.5, 110)])))
        self.assertIn("BEARISH_KICKER", detect_candlestick_patterns(bars([
            neutral, neutral, neutral, (100, 110, 99, 108), (99, 99.5, 90, 90)])))
        self.assertIn("THREE_INSIDE_UP", detect_candlestick_patterns(bars([
            neutral, neutral, (110, 111, 99, 100), (101, 107, 100.5, 106), (106, 113, 105, 112)])))
        self.assertIn("THREE_INSIDE_DOWN", detect_candlestick_patterns(bars([
            neutral, neutral, (100, 111, 99, 110), (109, 109.5, 103, 104), (104, 105, 97, 98)])))
        self.assertIn("THREE_OUTSIDE_UP", detect_candlestick_patterns(bars([
            neutral, neutral, (105, 106, 99, 100), (99, 108, 98, 107), (107, 111, 106, 110)])))
        self.assertIn("THREE_OUTSIDE_DOWN", detect_candlestick_patterns(bars([
            neutral, neutral, (100, 106, 99, 105), (106, 107, 98, 98), (98, 99, 94, 95)])))
        rising = bars([
            (100, 112, 99, 110), (110, 111, 102, 108),
            (108, 109, 101, 106), (106, 107, 100, 104),
            (105, 114, 104, 113),
        ])
        falling = bars([
            (110, 111, 98, 100), (100, 108, 99, 102),
            (102, 109, 101, 104), (104, 110, 103, 106),
            (105, 106, 94, 95),
        ])
        self.assertIn("RISING_THREE_METHODS", detect_candlestick_patterns(rising))
        self.assertIn("FALLING_THREE_METHODS", detect_candlestick_patterns(falling))
        features = dict(zip(MODEL_FEATURES, _feature_vector(rising)))
        self.assertEqual(features["rising_three_methods_5bar"], 1.0)
        self.assertIn("falling_three_methods_5bar", MODEL_FEATURES)
        soldiers = bars([
            neutral, neutral, (100, 105, 99, 104),
            (102, 108, 101, 107), (105, 112, 104, 111),
        ])
        crows = bars([
            neutral, neutral, (110, 111, 105, 106),
            (108, 109, 102, 103), (105, 106, 99, 100),
        ])
        self.assertIn("THREE_WHITE_SOLDIERS", detect_candlestick_patterns(soldiers))
        self.assertIn("THREE_BLACK_CROWS", detect_candlestick_patterns(crows))


    def test_market_context_is_causal_and_marks_missing_volume_unknown(self):
        candles = [
            {
                "open_time_ms": index * 3_600_000,
                "open": str(100 + index), "high": str(103 + index),
                "low": str(99 + index), "close": str(102 + index),
                "confirmed": True,
            }
            for index in range(CONTEXT_WINDOW_LENGTH)
        ]
        context = build_pattern_context(candles)
        self.assertEqual(context["version"], MARKET_CONTEXT_VERSION)
        self.assertEqual(context["history_bars"], CONTEXT_WINDOW_LENGTH)
        self.assertEqual(context["trend"], "BULLISH")
        self.assertIsNone(context["volume_ratio"])
        self.assertEqual(context["price_action_structure"]["order_block"]["status"], "UNKNOWN")
        self.assertEqual(context["support_resistance_scope"], "CONFIRMED_SWING_PIVOTS_FROM_50_COMPLETED_BARS")
        changed = [dict(item) for item in candles]
        changed[-1].update({"open": "150", "high": "152", "low": "149", "close": "150"})
        self.assertNotEqual(build_pattern_context(changed), context)
        with self.assertRaises(ValueError):
            build_pattern_context(candles[:-1])
        incomplete = [dict(item) for item in candles]
        incomplete[-1]["confirmed"] = False
        with self.assertRaises(ValueError):
            build_pattern_context(incomplete)

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
        self.assertEqual(stored_model["schema"], "CSR_CH01_CANDLE_PATTERN_PA_SOFTMAX_V4")
        self.assertEqual(stored_model["feature_window_length"], 5)
        self.assertIn("bullish_engulfing_2bar", stored_model["features"])
        self.assertIn("morning_star_3bar", stored_model["features"])
        self.assertIn("rising_three_methods_5bar", stored_model["features"])
        self.assertIn("falling_three_methods_5bar", stored_model["features"])
        self.assertEqual(len(stored_model["pattern_feature_mapping"]), 31)
        self.assertEqual(stored_model["pattern_feature_mapping"]["morning_star_3bar"], "MORNING_STAR")
        self.assertEqual(stored_model["pattern_catalog_version"], "CSR_CANDLE_HEURISTICS_5BAR_V2")
        self.assertEqual(stored_model["market_context_version"], MARKET_CONTEXT_VERSION)
        self.assertEqual(stored_model["market_context_scope"], "AUDIT_AND_REGIME_STRATIFICATION_ONLY_NOT_MODEL_FEATURES")
        self.assertEqual(stored_model["context_window_length"], CONTEXT_WINDOW_LENGTH)
        self.assertIn("MORNING_STAR", stored_model["pattern_catalog"])
        loaded_artifact, loaded_anchor = load_candidate_artifact(self.memory)
        self.assertEqual(loaded_artifact["artifact_sha256"], candidate["artifact_sha256"])
        self.assertEqual(loaded_anchor, "baseline-0")
        feature_window = self.memory.get_experience("baseline-0")["events"][0]["payload"]["input_snapshot"]["feature_window"]
        predicted, probabilities = predict_candidate(feature_window, loaded_artifact)
        self.assertIn(predicted, {"BULLISH_CANDLE", "BEARISH_CANDLE", "NEUTRAL_CANDLE"})
        self.assertAlmostEqual(sum(probabilities), 1.0, places=7)



if __name__ == "__main__":
    unittest.main()
