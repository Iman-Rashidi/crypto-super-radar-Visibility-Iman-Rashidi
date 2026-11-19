from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from crypto_super_radar.challenge1_entry_evaluation import (
    ENTRY_EVALUATION_HEARTBEAT_SECONDS,
    EntryEvaluationCancelled,
    MIN_ENTRY_EVALUATION_SECONDS,
    validate_challenge1_entry_revalidation,
    wait_for_minimum_entry_evaluation_window,
)


def candidate_record(*, bar_time: datetime, signal: str = "LONG", pattern: str = "Bullish Engulfing"):
    direction = "BULLISH" if signal == "LONG" else "BEARISH"
    bar_label = bar_time.isoformat()
    signal_id = f"challenge1-v4:BTC-USDT:5m:{bar_label}"
    return {
        "signal": signal,
        "signal_id": signal_id,
        "instrument_id": "BTC-USDT",
        "inst_id": "BTC-USDT",
        "signal_bar_open_utc": bar_label,
        "execution": {
            "status": "PASS",
            "direction": signal,
            "signal_id": signal_id,
            "signal_bar_open_utc": bar_label,
        },
        "challenge1_evidence": {
            "setup_timeframe": "5m",
            "completed_candles_only": True,
            "chosen_pattern": {
                "name_en": pattern,
                "direction": direction,
                "category": "CANDLESTICK",
                "timeframe": "5m",
            },
        },
    }


class EntryEvaluationWaitTests(unittest.TestCase):
    def test_waits_full_five_minutes_using_three_second_local_heartbeats(self):
        now = [12.0]
        sleeps = []
        progress = []

        def fake_sleep(seconds):
            sleeps.append(seconds)
            now[0] += seconds

        elapsed = wait_for_minimum_entry_evaluation_window(
            12.0,
            monotonic=lambda: now[0],
            sleep=fake_sleep,
            progress=progress.append,
        )

        self.assertGreaterEqual(elapsed, MIN_ENTRY_EVALUATION_SECONDS)
        self.assertEqual(sum(sleeps), MIN_ENTRY_EVALUATION_SECONDS)
        self.assertTrue(all(
            0 < delay <= ENTRY_EVALUATION_HEARTBEAT_SECONDS
            for delay in sleeps
        ))
        self.assertEqual(len(sleeps), 100)
        self.assertEqual(progress[0], 0.0)
        self.assertEqual(progress[-1], MIN_ENTRY_EVALUATION_SECONDS)

    def test_cancellation_stops_the_wait_before_the_window_is_complete(self):
        now = [0.0]
        checks = [0]

        def cancel_check():
            checks[0] += 1
            return checks[0] >= 2

        def fake_sleep(seconds):
            now[0] += seconds

        with self.assertRaises(EntryEvaluationCancelled):
            wait_for_minimum_entry_evaluation_window(
                0.0,
                cancel_check=cancel_check,
                monotonic=lambda: now[0],
                sleep=fake_sleep,
            )
        self.assertEqual(now[0], ENTRY_EVALUATION_HEARTBEAT_SECONDS)

    def test_rejects_invalid_monotonic_start(self):
        for value in (True, float("nan"), float("inf"), "12"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                wait_for_minimum_entry_evaluation_window(value)


class EntryEvaluationRevalidationTests(unittest.TestCase):
    def setUp(self):
        self.initial_time = datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc)
        self.final_time = self.initial_time + timedelta(minutes=5)
        self.initial = candidate_record(bar_time=self.initial_time)
        self.refreshed = candidate_record(bar_time=self.final_time)

    def validate(self, *, elapsed=300.1):
        return validate_challenge1_entry_revalidation(
            self.initial, self.refreshed, elapsed_seconds=elapsed,
        )

    def test_accepts_same_direction_and_pattern_on_a_new_completed_five_minute_candle(self):
        result = self.validate()
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["reason_codes"], [])
        self.assertTrue(result["direction_preserved"])
        self.assertTrue(result["pattern_preserved"])
        self.assertTrue(result["new_closed_candle_confirmed"])
        self.assertEqual(result["initial_signal_id"], self.initial["signal_id"])
        self.assertEqual(result["final_signal_id"], self.refreshed["signal_id"])

    def test_blocks_when_the_minimum_window_is_not_complete(self):
        result = self.validate(elapsed=299.999)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("MINIMUM_EVALUATION_WINDOW_NOT_MET", result["reason_codes"])

    def test_blocks_when_direction_changes(self):
        self.refreshed = candidate_record(
            bar_time=self.final_time, signal="SHORT", pattern="Bearish Engulfing",
        )
        result = self.validate()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("DIRECTION_NOT_PRESERVED", result["reason_codes"])

    def test_blocks_when_selected_pattern_changes(self):
        self.refreshed = candidate_record(
            bar_time=self.final_time, signal="LONG", pattern="Bullish Breakout",
        )
        result = self.validate()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("SELECTED_PATTERN_NOT_PRESERVED", result["reason_codes"])

    def test_blocks_when_the_same_candle_or_signal_id_is_reused(self):
        self.refreshed = candidate_record(bar_time=self.initial_time)
        result = self.validate()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("NEW_SIGNAL_ID_REQUIRED", result["reason_codes"])
        self.assertIn("NEWER_CLOSED_5M_CANDLE_NOT_CONFIRMED", result["reason_codes"])

    def test_blocks_when_either_round_is_not_a_closed_five_minute_setup(self):
        self.refreshed["challenge1_evidence"]["completed_candles_only"] = False
        result = self.validate()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("CLOSED_5M_CANDLE_EVIDENCE_REQUIRED", result["reason_codes"])

    def test_blocks_when_either_strategy_round_is_not_pass(self):
        self.refreshed["execution"]["status"] = "NO_TRADE"
        result = self.validate()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("ENTRY_SETUP_NOT_VALID_ON_BOTH_ROUNDS", result["reason_codes"])


if __name__ == "__main__":
    unittest.main()
