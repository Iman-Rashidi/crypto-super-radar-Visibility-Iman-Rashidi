from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

import pandas as pd

from crypto_super_radar.easy_trader_learning import (
    EasyTraderLearningMemory,
    FEATURE_NAMES,
    build_easy_trader_features,
    evaluate_flow_candle_signal,
)


def frame_with_prices(count: int = 20) -> pd.DataFrame:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = []
    for i in range(count):
        close = 100.0
        rows.append({
            "open": close - 0.2,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": 1000.0,
        })
    index = pd.date_range(start=start, periods=count, freq="1min")
    return pd.DataFrame(rows, index=index)


def flow_payload() -> dict:
    trades = [
        {"side": "BUY", "quote_value": 20_000.0},
        {"side": "BUY", "quote_value": 15_000.0},
        {"side": "SELL", "quote_value": 2_000.0},
    ]
    return {
        "pressure": {"buy_pct": 68.0, "sell_pct": 32.0, "buy_quote": 680_000.0, "sell_quote": 320_000.0},
        "aggressor_flow": {"buy_quote": 800_000.0, "sell_quote": 200_000.0, "net_delta_quote": 600_000.0},
        "order_book": {"status": "PASS", "imbalance_pct": 25.0},
        "large_trades": trades,
        "rvol": 1.8,
    }


def features(value: float = 0.5) -> dict[str, float]:
    return {
        "side_sign": 1.0,
        "candle_pressure": value,
        "aggressor_delta_ratio": value,
        "depth_imbalance": value,
        "large_print_imbalance": value,
        "rvol_scaled": 0.4,
        "candle_body_ratio": value,
        "candle_close_location": value,
        "pattern_score_scaled": value,
    }


class EasyTraderLearningTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.memory = EasyTraderLearningMemory(self.connection)

    def tearDown(self):
        self.connection.close()

    def test_flow_and_confirmed_candle_are_the_only_decision_inputs(self):
        frame = frame_with_prices()
        pattern = {
            "state": "CONFIRMED",
            "direction": "BULLISH",
            "score": 20,
            "name_en": "Bullish Engulfing",
            "name_fa": "پوشای صعودی",
            "invalidation_price": 98.0,
        }
        decision = evaluate_flow_candle_signal(
            frame, flow_payload(), [pattern], freshness_status="FRESH",
        )
        self.assertEqual(decision["signal"], "BUY")
        self.assertEqual(decision["status"], "CANDIDATE")
        self.assertEqual(decision["supporting_flow_count"], 4)
        vector = build_easy_trader_features(frame, flow_payload(), pattern, "BUY")
        self.assertEqual(set(vector), set(FEATURE_NAMES))
        self.assertGreater(vector["candle_pressure"], 0)
        self.assertGreater(vector["aggressor_delta_ratio"], 0)

    def test_unconfirmed_pattern_never_becomes_buy_or_sell(self):
        frame = frame_with_prices()
        pattern = {
            "state": "NEEDS_CONFIRMATION",
            "direction": "NEUTRAL",
            "potential_direction": "BULLISH",
            "score": 8,
            "name_en": "Morning Star",
        }
        decision = evaluate_flow_candle_signal(
            frame, flow_payload(), [pattern], freshness_status="FRESH",
        )
        self.assertEqual(decision["signal"], "WAIT")
        self.assertNotEqual(decision["status"], "CANDIDATE")

    def test_stale_candle_blocks_easy_signal(self):
        decision = evaluate_flow_candle_signal(
            frame_with_prices(), flow_payload(),
            [{"state": "CONFIRMED", "direction": "BULLISH", "score": 20}],
            freshness_status="STALE",
        )
        self.assertEqual(decision["status"], "BLOCKED")
        self.assertEqual(decision["signal"], "WAIT")

    def test_observations_are_idempotent_and_wait_samples_are_stored(self):
        stamp = "2026-01-01T00:00:00+00:00"
        kwargs = {
            "symbol": "BTCUSDT", "interval": "1m", "candle_at": stamp,
            "observed_at_ms": 1_767_225_600_000, "reference_price": 100.0,
            "side": "WAIT", "features": features(),
        }
        first = self.memory.record_observation(**kwargs)
        second = self.memory.record_observation(**kwargs)
        self.assertTrue(first["inserted"])
        self.assertFalse(second["inserted"])
        snapshot = self.memory.snapshot("BTCUSDT", "1m")
        self.assertEqual(snapshot["sample_count"], 1)
        self.assertEqual(snapshot["labelled_count"], 0)
        self.assertFalse(snapshot["demo_or_live_orders_enabled"])

    def test_outcomes_are_labelled_only_after_three_future_candles_close(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        stamps = [(start + timedelta(minutes=i)).isoformat() for i in range(5)]
        self.memory.record_observation(
            symbol="BTCUSDT", interval="1m", candle_at=stamps[0],
            observed_at_ms=1_767_225_600_000, reference_price=100.0,
            side="BUY", features=features(),
        )
        early = self.memory.label_matured(
            symbol="BTCUSDT", interval="1m",
            completed_closes=[(stamps[0], 100.0), (stamps[1], 100.1), (stamps[2], 100.2)],
        )
        self.assertEqual(early["labelled"], 0)
        mature = self.memory.label_matured(
            symbol="BTCUSDT", interval="1m",
            completed_closes=[
                (stamps[0], 100.0), (stamps[1], 100.1), (stamps[2], 100.2),
                (stamps[3], 101.0), (stamps[4], 101.2),
            ],
        )
        self.assertEqual(mature["labelled"], 1)
        self.assertEqual(self.memory.snapshot("BTCUSDT", "1m")["labelled_count"], 1)
        repeated = self.memory.label_matured(
            symbol="BTCUSDT", interval="1m",
            completed_closes=[(stamp, price) for stamp, price in [
                (stamps[0], 100.0), (stamps[1], 100.1), (stamps[2], 100.2),
                (stamps[3], 101.0), (stamps[4], 101.2),
            ]],
        )
        self.assertEqual(repeated["labelled"], 0)

    def test_poor_outcomes_escalate_controls_without_deleting_memory(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        closes = []
        for i in range(83):
            stamp = (start + timedelta(minutes=i)).isoformat()
            closes.append((stamp, 100.0))
        for sample in range(20):
            pos = sample * 4
            stamp = closes[pos][0]
            self.memory.record_observation(
                symbol="BTCUSDT", interval="1m", candle_at=stamp,
                observed_at_ms=1_767_225_600_000 + sample * 60_000,
                reference_price=100.0, side="BUY", features=features(-0.5),
            )
            closes[pos + 3] = (closes[pos + 3][0], 98.0)
        labelled = self.memory.label_matured(
            symbol="BTCUSDT", interval="1m", completed_closes=closes,
            round_trip_cost_pct=0.15,
        )
        self.assertEqual(labelled["labelled"], 20)
        state = self.memory.snapshot("BTCUSDT", "1m")
        self.assertGreaterEqual(state["control_level"], 2)
        self.assertEqual(state["sample_count"], 20)
        self.assertEqual(state["labelled_count"], 20)

    def test_repeated_operational_errors_escalate_to_freeze_level(self):
        self.memory.record_error(symbol="BTCUSDT", interval="5m", code="STALE_FEED")
        second = self.memory.record_error(symbol="BTCUSDT", interval="5m", code="STALE_FEED")
        third = self.memory.record_error(symbol="BTCUSDT", interval="5m", code="STALE_FEED")
        self.assertEqual(second["control_level"], 4)
        self.assertEqual(third["control_level"], 5)
        self.assertEqual(self.memory.snapshot("BTCUSDT", "5m")["control_level"], 5)

    def test_local_model_is_trained_on_chronological_holdout_without_ai(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        closes = []
        sample_count = 120
        for i in range(sample_count * 4 + 3):
            closes.append(((start + timedelta(minutes=i)).isoformat(), 100.0))
        for sample in range(sample_count):
            pos = sample * 4
            stamp = closes[pos][0]
            win = sample % 2 == 0
            x = 0.8 if win else -0.8
            self.memory.record_observation(
                symbol="ETHUSDT", interval="1m", candle_at=stamp,
                observed_at_ms=1_767_225_600_000 + sample * 60_000,
                reference_price=100.0, side="BUY", features=features(x),
            )
            closes[pos + 3] = (closes[pos + 3][0], 101.5 if win else 98.5)
        self.memory.label_matured(
            symbol="ETHUSDT", interval="1m", completed_closes=closes,
        )
        result = self.memory.train_if_ready("ETHUSDT", "1m")
        self.assertEqual(result["status"], "ACCEPTED")
        self.assertEqual(result["model_kind"], "LOGISTIC")
        prediction = self.memory.predict("ETHUSDT", "1m", features(0.8))
        self.assertEqual(prediction["status"], "PREDICTED")
        self.assertGreater(prediction["probability"], 0.5)
        state = self.memory.snapshot("ETHUSDT", "1m")
        self.assertEqual(state["active_model_kind"], "LOGISTIC")
        self.assertEqual(state["ai_dependency"], "NONE")
        self.assertFalse(state["demo_or_live_orders_enabled"])


if __name__ == "__main__":
    unittest.main()
