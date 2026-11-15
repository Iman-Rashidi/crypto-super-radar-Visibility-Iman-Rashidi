from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from crypto_super_radar.challenge_01_ai_mentor import review_locked_baseline
from crypto_super_radar.challenge_01_assisted_runner import Challenge01AssistedRunner
from crypto_super_radar.challenge_01_runner import BAR_MILLISECONDS
from crypto_super_radar.learning_experience_memory import LearningExperienceMemory


def _candle(ts: int, kind: int) -> dict:
    close = {1: "102", 0: "100", -1: "98"}[kind]
    return {
        "open_time_ms": ts,
        "open": "100",
        "high": "104",
        "low": "96",
        "close": close,
        "confirmed": True,
    }


def _row(candle: dict) -> list[str]:
    return [
        str(candle["open_time_ms"]),
        candle["open"],
        candle["high"],
        candle["low"],
        candle["close"],
        "10", "10", "10", "1",
    ]


class FakeFutureSpotClient:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def get_candles(self, inst_id, *, bar, limit, after=None, before=None):
        self.calls.append({
            "inst_id": inst_id,
            "bar": bar,
            "limit": limit,
            "after": after,
            "before": before,
        })
        return self.rows[:limit]


class Challenge01AssistedRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tempdir.name) / "assisted.sqlite3")
        self.base_time = 1_800_000_000_000
        self.candles = [
            _candle(self.base_time + index * BAR_MILLISECONDS, [1, 0, -1][index % 3])
            for index in range(101)
        ]
        self.last_target_time = self.candles[-1]["open_time_ms"]
        self.future_candles = [
            _candle(self.last_target_time + index * BAR_MILLISECONDS, [1, 0, -1][(100 + index) % 3])
            for index in range(1, 106)
        ]
        self.client = FakeFutureSpotClient([_row(c) for c in reversed(self.future_candles)])

    def tearDown(self):
        self.tempdir.cleanup()

    def connection_factory(self):
        db = sqlite3.connect(self.db_path)
        db.row_factory = sqlite3.Row
        return db

    def seed_locked_baseline(self):
        db = self.connection_factory()
        try:
            memory = LearningExperienceMemory(db)
            with db:
                for index in range(100):
                    feature = self.candles[index]
                    target = self.candles[index + 1]
                    feature_ts = feature["open_time_ms"]
                    target_ts = target["open_time_ms"]
                    label = "BULLISH_CANDLE" if float(target["close"]) > 100 else (
                        "BEARISH_CANDLE" if float(target["close"]) < 100 else "NEUTRAL_CANDLE"
                    )
                    baseline_prediction = "BULLISH_CANDLE" if float(feature["close"]) > 100 else (
                        "BEARISH_CANDLE" if float(feature["close"]) < 100 else "NEUTRAL_CANDLE"
                    )
                    action = "PREDICT_NEXT_" + baseline_prediction.removesuffix("_CANDLE")
                    experience_id = f"seed-{index:03d}"
                    memory.record_decision(
                        experience_id=experience_id,
                        event_id=f"{experience_id}:decision",
                        engine_id="CH01_SINGLE_CANDLE",
                        challenge_id="CHALLENGE-01",
                        cycle_id=f"CH01-R01:BTC-USDT:1H:{target_ts}",
                        decision_mode="BOT_ONLY",
                        environment="BLOFIN_DEMO",
                        market="SPOT",
                        symbol="BTC-USDT",
                        timeframe="1H",
                        model_version="ch01-momentum-baseline-v2",
                        dataset_version="round-1-seed",
                        decision={
                            "candle_classification": baseline_prediction,
                            "action": action,
                            "rule": "previous_candle_body_direction",
                            "prediction_horizon_bars": 1,
                            "confidence": None,
                        },
                        input_snapshot={
                            "round_number": 1,
                            "candle_open_time_ms": target_ts,
                            "feature_candle_open_time_ms": feature_ts,
                            "target_candle_open_time_ms": target_ts,
                            "feature_candle": feature,
                            "prediction_horizon_bars": 1,
                            "data_quality": "VALID_COMPLETED_FEATURE_AND_TARGET",
                            "target_candle_in_input": False,
                        },
                    )
                    memory.record_outcome(
                        experience_id=experience_id,
                        event_id=f"{experience_id}:outcome",
                        decision_correctness="CORRECT" if baseline_prediction == label else "INCORRECT",
                        realized_result="NOT_EXECUTED",
                        realized_net_pnl=None,
                        final=True,
                        evidence={
                            "source": "blofin_demo_spot_next_completed_candle",
                            "comparison": "NEXT_CANDLE_DIRECTION_FROM_NEXT_COMPLETED_BAR",
                            "feature_candle_open_time_ms": feature_ts,
                            "target_candle_open_time_ms": target_ts,
                            "target_candle": target,
                            "reference_classification": label,
                            "prediction_horizon_bars": 1,
                            "no_future_data_used_in_input": True,
                        },
                    )
        finally:
            db.close()
        result = review_locked_baseline(
            connection_factory=self.connection_factory,
            backend_factory=lambda: "TEST_SQLITE_FILE",
            allow_non_durable_for_tests=True,
        )
        self.assertEqual(result["status"], "MENTOR_REVIEW_RECORDED", msg=repr(result))
        return result

    def test_round_two_pairs_bot_only_and_mentor_on_newer_non_overlapping_targets(self):
        candidate = self.seed_locked_baseline()
        runner = Challenge01AssistedRunner(
            client=self.client,
            connection_factory=self.connection_factory,
            backend_factory=lambda: "TEST_SQLITE_FILE",
            allow_non_durable_for_tests=True,
        )
        result = runner.run_round(round_number=2, sample_count=100)
        self.assertEqual(result["status"], "ROUND_RECORDED_NOT_MASTERY", msg=repr(result))
        self.assertEqual(result["sample_count"], 100)
        self.assertEqual(result["memory_records_verified"], 200)
        self.assertEqual(result["memory_integrity"], "PASS")
        self.assertEqual(result["model_artifact_sha256"], candidate["model_candidate"]["artifact_sha256"])
        self.assertEqual(self.client.calls[0]["before"], self.last_target_time)
        self.assertEqual(result["round_status"], "EVALUATED_NOT_MASTERY")
        self.assertFalse(result["mastery_awarded"])
        self.assertFalse(result["orders_submitted"])

        db = self.connection_factory()
        try:
            memory = LearningExperienceMemory(db)
            events = memory.recent_experiences(limit=500, challenge_id="CHALLENGE-01")
            round_two = []
            for item in events:
                decision = next((e for e in item["events"] if e["event_type"] == "DECISION_RECORDED"), None)
                if decision and decision["payload"]["input_snapshot"].get("round_number") == 2:
                    round_two.append(item)
            self.assertEqual(len(round_two), 200)
            self.assertEqual(
                {item["decision_mode"] for item in round_two},
                {"BOT_ONLY", "BOT_WITH_AI_MENTOR"},
            )
            assisted = next(item for item in round_two if item["decision_mode"] == "BOT_WITH_AI_MENTOR")
            decision_event = next(e for e in assisted["events"] if e["event_type"] == "DECISION_RECORDED")
            snapshot = decision_event["payload"]["input_snapshot"]
            self.assertFalse(snapshot["target_candle_in_input"])
            self.assertNotIn("target_candle", snapshot)
            self.assertEqual(
                snapshot["mentor_model_artifact_sha256"],
                candidate["model_candidate"]["artifact_sha256"],
            )
        finally:
            db.close()

    def test_round_two_blocks_without_100_verified_prior_round_targets(self):
        runner = Challenge01AssistedRunner(
            client=self.client,
            connection_factory=self.connection_factory,
            backend_factory=lambda: "TEST_SQLITE_FILE",
            allow_non_durable_for_tests=True,
        )
        result = runner.run_round(round_number=2, sample_count=100)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["reason_code"], "PRIOR_ROUND_MUST_HAVE_100_DISTINCT_TARGETS")
        self.assertEqual(self.client.calls, [])
        self.assertFalse(result["orders_submitted"])


if __name__ == "__main__":
    unittest.main()
