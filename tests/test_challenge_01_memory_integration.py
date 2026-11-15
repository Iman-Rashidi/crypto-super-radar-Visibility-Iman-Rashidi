from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from crypto_super_radar.challenge_01_runner import (
    Challenge01DataError,
    Challenge01Runner,
    MIN_SAMPLES_PER_ROUND,
    get_challenge_01_status,
)
from crypto_super_radar.learning_experience_memory import LearningExperienceMemory
from crypto_super_radar.blofin_demo_spot_readonly import (
    BloFinDemoSpotReadOnly,
    DEMO_SPOT_REST_URL,
)


def candle(ts: int, opening: str, high: str, low: str, closing: str, confirm: str = "1"):
    return [str(ts), opening, high, low, closing, "10", "10", "10", confirm]


class FakeSpotClient:
    def __init__(self, rows, older_rows=None):
        self.rows = rows
        self.older_rows = older_rows
        self.calls = []

    def get_candles(self, inst_id, *, bar, limit, after=None):
        self.calls.append({"inst_id": inst_id, "bar": bar, "limit": limit, "after": after})
        return self.older_rows if after is not None and self.older_rows is not None else self.rows


class Challenge01MemoryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tempdir.name) / "challenge01.sqlite3")
        self.now_ms = 1_800_000_000_000
        self.rows = [
            candle(self.now_ms - 60 * 60 * 1000 - index * 60 * 60 * 1000,
                   "100", "105", "95", "101" if index % 3 == 0 else ("99" if index % 3 == 1 else "100"))
            for index in range(MIN_SAMPLES_PER_ROUND + 5)
        ]
        self.older_rows = [
            candle(self.now_ms - (MIN_SAMPLES_PER_ROUND + 1 + index) * 60 * 60 * 1000,
                   "100", "105", "95", "101" if index % 3 == 0 else "99")
            for index in range(MIN_SAMPLES_PER_ROUND + 5)
        ]

    def tearDown(self):
        self.tempdir.cleanup()

    def connection_factory(self):
        db = sqlite3.connect(self.db_path)
        db.row_factory = sqlite3.Row
        return db

    def runner(self, rows=None):
        return Challenge01Runner(
            client=FakeSpotClient(rows if rows is not None else self.rows, older_rows=self.older_rows),
            connection_factory=self.connection_factory,
            backend_factory=lambda: "TEST_SQLITE_FILE",
            now_ms=lambda: self.now_ms,
            allow_non_durable_for_tests=True,
        )

    def test_real_memory_write_close_reopen_and_verify_100_candles(self):
        result = self.runner().run_bot_only_round(round_number=1, sample_count=100)
        self.assertEqual(result["status"], "BASELINE_RECORDED")
        self.assertEqual(result["sample_count"], 100)
        self.assertTrue(result["memory_recovery_verified"])
        self.assertEqual(result["memory_records_verified"], 100)
        self.assertEqual(result["memory_integrity"], "PASS")
        self.assertEqual(result["memory_backend"], "TEST_SQLITE_FILE")
        self.assertEqual(result["balanced_accuracy"], 1.0)
        self.assertEqual(result["macro_f1"], 1.0)
        self.assertEqual(result["signal_precision"], 1.0)
        self.assertEqual(result["metrics_scope"], "CLOSED_CANDLE_OHLC_CLASSIFICATION_ONLY_NOT_FORWARD_PREDICTION_OR_PNL")
        self.assertEqual(result["next_stage"], "AI_MENTOR_REQUIRED")
        self.assertTrue(result["baseline_locked"])
        self.assertFalse(result["orders_submitted"])

        db = self.connection_factory()
        try:
            memory = LearningExperienceMemory(db)
            saved = memory.get_experience(result["first_experience_id"])
            decision = next(event for event in saved["events"] if event["event_type"] == "DECISION_RECORDED")
            outcome = next(event for event in saved["events"] if event["event_type"] == "OUTCOME_LABELLED")
            self.assertEqual(decision["payload"]["decision"]["action"], "BUY_CANDIDATE")
            self.assertEqual(decision["payload"]["decision"]["confidence_scope"], "DETERMINISTIC_CLOSED_CANDLE_CLASSIFICATION_NOT_MARKET_FORECAST")
            self.assertEqual(outcome["payload"]["decision_correctness"], "CORRECT")
            self.assertEqual(outcome["payload"]["realized_result"], "NOT_EXECUTED")
            self.assertEqual(
                outcome["payload"]["evidence"]["comparison"],
                "DETERMINISTIC_OHLC_LABEL_CONSISTENCY_NOT_PREDICTIVE_GROUND_TRUTH",
            )
        finally:
            db.close()

    def test_runtime_runner_blocks_local_sqlite_before_provider_access(self):
        client = FakeSpotClient(self.rows)
        runner = Challenge01Runner(
            client=client,
            connection_factory=self.connection_factory,
            backend_factory=lambda: "TEST_SQLITE_FILE",
            now_ms=lambda: self.now_ms,
        )
        result = runner.run_bot_only_round(round_number=1, sample_count=100)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["reason_code"], "DURABLE_LEARNING_MEMORY_REQUIRED")
        self.assertEqual(result["memory_backend"], "TEST_SQLITE_FILE")
        self.assertEqual(client.calls, [])
        self.assertFalse(result["orders_submitted"])

    def test_status_endpoint_never_reports_local_sqlite_as_durable(self):
        status = get_challenge_01_status(
            connection_factory=self.connection_factory,
            backend_factory=lambda: "TEST_SQLITE_FILE",
        )
        self.assertEqual(status["status"], "BLOCKED")
        self.assertEqual(status["reason_code"], "DURABLE_LEARNING_MEMORY_REQUIRED")
        self.assertEqual(status["memory_backend"], "TEST_SQLITE_FILE")
        self.assertIsNone(status["next_round"])
        self.assertFalse(status["orders_submitted"])

    def test_baseline_locks_stage_and_later_bot_only_rounds_are_blocked(self):
        runner = self.runner()
        baseline = runner.run_bot_only_round(round_number=1, sample_count=100)
        self.assertEqual(baseline["status"], "BASELINE_RECORDED")

        status = get_challenge_01_status(
            connection_factory=self.connection_factory,
            backend_factory=lambda: "TEST_SQLITE_FILE",
            allow_non_durable_for_tests=True,
        )
        self.assertEqual(status["status"], "PASS")
        self.assertTrue(status["baseline_locked"])
        self.assertEqual(status["baseline_samples"], 100)
        self.assertIsNone(status["next_round"])
        self.assertEqual(status["next_stage"], "AI_MENTOR_REQUIRED")
        self.assertTrue(status["later_rounds_blocked"])

        calls_before = len(runner.client.calls)
        blocked = runner.run_bot_only_round(round_number=2, sample_count=100)
        self.assertEqual(blocked["status"], "BLOCKED")
        self.assertEqual(blocked["reason_code"], "AI_MENTOR_PHASE_REQUIRED")
        self.assertEqual(len(runner.client.calls), calls_before)
        self.assertFalse(blocked["orders_submitted"])

    def test_open_latest_candle_is_excluded_and_completed_candles_are_used(self):
        rows = [candle(self.now_ms - 5 * 60 * 1000, "100", "106", "99", "105", "0")] + self.rows
        result = self.runner(rows).run_bot_only_round(round_number=1, sample_count=100)
        self.assertEqual(result["status"], "BASELINE_RECORDED")
        self.assertEqual(result["sample_count"], 100)

    def test_duplicate_timestamp_is_recorded_unknown_and_never_signals(self):
        rows = list(self.rows)
        rows[2] = list(rows[1])
        result = self.runner(rows).run_bot_only_round(round_number=1, sample_count=100)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["reason_code"], "DUPLICATE_CANDLE_TIMESTAMP")
        self.assertFalse(result["orders_submitted"])

    def test_reversed_timestamp_order_is_recorded_unknown(self):
        rows = list(reversed(self.rows))
        result = self.runner(rows).run_bot_only_round(round_number=1, sample_count=100)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["reason_code"], "CANDLE_ORDER_NOT_NEWEST_FIRST")

    def test_inconsistent_ohlc_is_rejected(self):
        rows = list(self.rows)
        rows[0] = candle(self.now_ms - 60 * 60 * 1000, "100", "99", "95", "101")
        result = self.runner(rows).run_bot_only_round(round_number=1, sample_count=100)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["reason_code"], "INCONSISTENT_HIGH")

    def test_stale_latest_completed_candle_is_rejected(self):
        stale_now = self.now_ms + 6 * 60 * 60 * 1000
        runner = Challenge01Runner(
            client=FakeSpotClient(self.rows),
            connection_factory=self.connection_factory,
            backend_factory=lambda: "TEST_SQLITE_FILE",
            now_ms=lambda: stale_now,
            allow_non_durable_for_tests=True,
        )
        result = runner.run_bot_only_round(round_number=1, sample_count=100)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["reason_code"], "LATEST_COMPLETED_CANDLE_STALE")

    def test_second_round_is_locked_until_ai_mentor_phase(self):
        runner = self.runner()
        first = runner.run_bot_only_round(round_number=1, sample_count=100)
        calls_before = len(runner.client.calls)
        second = runner.run_bot_only_round(round_number=2, sample_count=100)
        self.assertEqual(first["status"], "BASELINE_RECORDED")
        self.assertEqual(second["status"], "BLOCKED")
        self.assertEqual(second["reason_code"], "AI_MENTOR_PHASE_REQUIRED")
        self.assertEqual(len(runner.client.calls), calls_before)
        self.assertFalse(second["orders_submitted"])

    def test_round_requires_100_samples(self):
        with self.assertRaises(ValueError):
            self.runner().run_bot_only_round(round_number=1, sample_count=99)


class BloFinDemoSpotAdapterTests(unittest.TestCase):
    def test_public_market_data_uses_demo_spot_endpoint_without_auth(self):
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"code": "0", "data": []}

        class Session:
            def __init__(self):
                self.call = None

            def get(self, url, *, params, timeout):
                self.call = (url, params, timeout)
                return Response()

        session = Session()
        client = BloFinDemoSpotReadOnly(session=session)
        self.assertEqual(client.get_candles("BTC-USDT", bar="1H", limit=120), [])
        url, params, _timeout = session.call
        self.assertEqual(url, f"{DEMO_SPOT_REST_URL}/api/v1/spot/market/candles")
        self.assertEqual(params["instType"], "SPOT")
        self.assertEqual(params["instId"], "BTC-USDT")
        self.assertEqual(params["bar"], "1H")


if __name__ == "__main__":
    unittest.main()
