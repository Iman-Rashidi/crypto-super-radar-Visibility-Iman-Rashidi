from __future__ import annotations

import unittest
from unittest.mock import patch
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from crypto_super_radar.market_patterns import _divergence, analyze_market_patterns


def candle_frame(direction: str = "down", count: int = 70) -> pd.DataFrame:
    rows = []
    for i in range(count):
        if direction == "down":
            close = 120.0 - i * 0.35
        elif direction == "up":
            close = 80.0 + i * 0.45
        else:
            close = 100.0 + (0.08 if i % 2 else -0.08)
        opened = close + (0.18 if direction == "down" else -0.18 if direction == "up" else 0.01)
        high = max(opened, close) + 0.20
        low = min(opened, close) - 0.20
        rows.append({
            "open": opened,
            "high": high,
            "low": low,
            "close": close,
            "volume": 1000.0,
        })
    index = pd.date_range(
        start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        periods=count,
        freq="5min",
    )
    return pd.DataFrame(rows, index=index)


class MarketPatternTests(unittest.TestCase):
    def test_hammer_after_downtrend_is_labelled_bullish_but_unconfirmed(self):
        frame = candle_frame("down")
        frame.iloc[-1, frame.columns.get_loc("open")] = 96.2
        frame.iloc[-1, frame.columns.get_loc("high")] = 96.3
        frame.iloc[-1, frame.columns.get_loc("low")] = 93.0
        frame.iloc[-1, frame.columns.get_loc("close")] = 96.0
        analysis = analyze_market_patterns(frame, [])
        hammer = next(p for p in analysis["candlestick_patterns"] if p["name_en"] == "Hammer")
        self.assertEqual(hammer["direction"], "BULLISH")
        self.assertEqual(hammer["state"], "NEEDS_CONFIRMATION")
        self.assertEqual(analysis["status"], "PASS")

    def test_doji_is_reported_as_neutral_not_as_a_trade_signal(self):
        frame = candle_frame("flat")
        frame.iloc[-1, frame.columns.get_loc("open")] = 100.0
        frame.iloc[-1, frame.columns.get_loc("high")] = 100.5
        frame.iloc[-1, frame.columns.get_loc("low")] = 99.5
        frame.iloc[-1, frame.columns.get_loc("close")] = 100.01
        analysis = analyze_market_patterns(frame, [])
        doji = next(p for p in analysis["candlestick_patterns"] if p["name_en"] == "Doji")
        self.assertEqual(doji["direction"], "NEUTRAL")
        self.assertEqual(doji["score"], 0)

    def test_bullish_engulfing_uses_immediately_previous_candle(self):
        frame = candle_frame("down")
        frame.iloc[-2] = [98.0, 98.2, 96.8, 97.0, 1000.0]
        frame.iloc[-1] = [96.7, 99.2, 96.5, 98.5, 1400.0]
        analysis = analyze_market_patterns(frame, [])
        engulfing = next(
            p for p in analysis["candlestick_patterns"]
            if p["name_en"] == "Bullish Engulfing"
        )
        self.assertEqual(engulfing["direction"], "BULLISH")
        self.assertEqual(engulfing["state"], "NEEDS_CONFIRMATION")

    def test_confirmed_double_bottom_requires_neckline_break(self):
        frame = candle_frame("flat")
        frame.iloc[-1, frame.columns.get_loc("open")] = 111.5
        frame.iloc[-1, frame.columns.get_loc("high")] = 112.5
        frame.iloc[-1, frame.columns.get_loc("low")] = 111.0
        frame.iloc[-1, frame.columns.get_loc("close")] = 112.0
        pivots = [
            {"kind": "LOW", "index": 10, "price": 100.0},
            {"kind": "HIGH", "index": 20, "price": 110.0},
            {"kind": "LOW", "index": 30, "price": 100.3},
        ]
        analysis = analyze_market_patterns(frame, pivots)
        double_bottom = next(
            p for p in analysis["geometric_patterns"] if p["name_en"] == "Double Bottom"
        )
        self.assertEqual(double_bottom["state"], "CONFIRMED")
        self.assertEqual(double_bottom["direction"], "BULLISH")

    def test_trend_momentum_and_wave_structure_are_returned(self):
        frame = candle_frame("up")
        analysis = analyze_market_patterns(frame, [], {
            "pattern": "BULLISH",
            "status": "CANDIDATE",
            "phase": "WAVE_3_CONFIRMED",
            "wave_count": 3,
            "next_wave": 4,
            "invalidation_price": 90.0,
            "reason": "Synthetic structural test",
        })
        direction = analysis["direction"]
        self.assertEqual(direction["trend"], "BULLISH")
        self.assertEqual(direction["bias"], "BULLISH")
        self.assertIsNotNone(direction["rsi14"])
        self.assertIsNotNone(direction["macd_histogram"])
        self.assertEqual(analysis["wave_structure"]["pattern"], "BULLISH")
        self.assertEqual(analysis["wave_structure"]["next_wave"], 4)

    def test_short_history_fails_closed(self):
        analysis = analyze_market_patterns(candle_frame("up", count=25), [])
        self.assertEqual(analysis["status"], "INSUFFICIENT_DATA")
        self.assertEqual(analysis["direction"]["bias"], "UNKNOWN")
        self.assertEqual(analysis["candlestick_patterns"], [])


    def test_adx_ema200_and_divergence_fields_are_reported(self):
        analysis = analyze_market_patterns(candle_frame("up", count=240), [])
        direction = analysis["direction"]
        self.assertIsNotNone(direction["adx14"])
        self.assertGreater(direction["adx14"], 25)
        self.assertIsNotNone(direction["ema200"])
        self.assertIn("divergence", direction)
        self.assertIn("fibonacci", direction)

    def test_divergence_exposes_exact_pivot_candle_coordinates(self):
        frame = candle_frame("up", count=50)
        rsi_values = pd.Series(50.0, index=frame.index)
        rsi_values.iloc[10] = 62.0
        rsi_values.iloc[30] = 54.0
        pivots = [
            {"kind": "HIGH", "index": 10, "price": 100.0},
            {"kind": "HIGH", "index": 30, "price": 110.0},
        ]
        with patch("crypto_super_radar.market_patterns._rsi_series", return_value=rsi_values):
            result = _divergence(frame, pivots)
        self.assertEqual(result["type"], "BEARISH")
        self.assertEqual(result["price_pivot_1_index"], 10)
        self.assertEqual(result["price_pivot_2_index"], 30)
        self.assertEqual(result["price_pivot_1_time"], frame.index[10].isoformat())
        self.assertEqual(result["price_pivot_2_time"], frame.index[30].isoformat())

    def test_fibonacci_golden_zone_and_invalidation_are_calculated(self):
        pivots = [
            {"kind": "LOW", "index": 10, "price": 100.0},
            {"kind": "HIGH", "index": 30, "price": 120.0},
            {"kind": "LOW", "index": 50, "price": 109.0},
        ]
        analysis = analyze_market_patterns(candle_frame("up"), pivots)
        fib = analysis["direction"]["fibonacci"]
        self.assertEqual(fib["status"], "GOLDEN_ZONE_CANDIDATE")
        self.assertAlmostEqual(fib["retracement"], 0.55)
        self.assertEqual(fib["invalidation_price"], 100.0)
        self.assertTrue(fib["confirmation_required"])


    def _set_three_inside_down_candles(self, frame: pd.DataFrame) -> pd.DataFrame:
        frame.iloc[-3] = [110.0, 112.0, 109.0, 111.8, 1000.0]
        frame.iloc[-2] = [111.4, 111.6, 110.5, 110.8, 1000.0]
        frame.iloc[-1] = [110.4, 110.6, 107.5, 108.5, 1500.0]
        return frame

    def test_three_inside_down_is_confirmed_only_after_close_below_first_low(self):
        frame = self._set_three_inside_down_candles(candle_frame("up"))
        analysis = analyze_market_patterns(frame, [])
        pattern = next(
            p for p in analysis["candlestick_patterns"]
            if p["name_en"] == "Three Inside Down"
        )
        self.assertEqual(pattern["direction"], "BEARISH")
        self.assertEqual(pattern["state"], "CONFIRMED")
        self.assertEqual(pattern["score"], -16)
        self.assertEqual(pattern["trigger_price"], 109.0)
        self.assertEqual(pattern["invalidation_price"], 112.0)

    def test_three_inside_down_requires_prior_uptrend(self):
        frame = self._set_three_inside_down_candles(candle_frame("flat"))
        analysis = analyze_market_patterns(frame, [])
        self.assertNotIn(
            "Three Inside Down",
            [p["name_en"] for p in analysis["candlestick_patterns"]],
        )

    def test_three_inside_down_requires_third_close_below_first_low(self):
        frame = self._set_three_inside_down_candles(candle_frame("up"))
        frame.iloc[-1, frame.columns.get_loc("close")] = 109.2
        analysis = analyze_market_patterns(frame, [])
        self.assertNotIn(
            "Three Inside Down",
            [p["name_en"] for p in analysis["candlestick_patterns"]],
        )

    def test_three_inside_down_requires_bearish_harami_inside_first_body(self):
        frame = self._set_three_inside_down_candles(candle_frame("up"))
        frame.iloc[-2, frame.columns.get_loc("close")] = 109.8
        analysis = analyze_market_patterns(frame, [])
        self.assertNotIn(
            "Three Inside Down",
            [p["name_en"] for p in analysis["candlestick_patterns"]],
        )



    def _set_gravestone_doji(self, frame: pd.DataFrame) -> pd.DataFrame:
        frame.iloc[-1] = [110.0, 116.0, 109.9, 110.1, 1500.0]
        return frame

    def test_gravestone_doji_after_uptrend_is_bearish_pending_confirmation(self):
        frame = self._set_gravestone_doji(candle_frame("up"))
        analysis = analyze_market_patterns(frame, [])
        pattern = next(
            p for p in analysis["candlestick_patterns"]
            if p["name_en"] == "Gravestone Doji"
        )
        self.assertEqual(pattern["direction"], "BEARISH")
        self.assertEqual(pattern["state"], "NEEDS_CONFIRMATION")
        self.assertEqual(pattern["score"], -11)
        self.assertEqual(pattern["trigger_price"], 116.0)
        self.assertEqual(pattern["invalidation_price"], 109.9)

    def test_gravestone_doji_without_uptrend_is_not_bearish(self):
        frame = self._set_gravestone_doji(candle_frame("flat"))
        analysis = analyze_market_patterns(frame, [])
        pattern = next(
            p for p in analysis["candlestick_patterns"]
            if p["name_en"] == "Gravestone Doji"
        )
        self.assertEqual(pattern["direction"], "NEUTRAL")
        self.assertEqual(pattern["score"], 0)

    def test_gravestone_doji_requires_long_upper_and_tiny_lower_wicks(self):
        frame = self._set_gravestone_doji(candle_frame("up"))
        frame.iloc[-1, frame.columns.get_loc("low")] = 108.0
        analysis = analyze_market_patterns(frame, [])
        self.assertNotIn(
            "Gravestone Doji",
            [p["name_en"] for p in analysis["candlestick_patterns"]],
        )



if __name__ == "__main__":
    unittest.main()
