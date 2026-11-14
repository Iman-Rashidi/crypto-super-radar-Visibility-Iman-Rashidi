from __future__ import annotations

import unittest
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from crypto_super_radar.market_patterns import analyze_market_patterns


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

    def _set_morning_star(self, frame: pd.DataFrame, *, with_confirmation: bool = False) -> pd.DataFrame:
        pattern_start = -4 if with_confirmation else -3
        frame.iloc[pattern_start] = [96.0, 96.5, 91.0, 91.5, 1100.0]
        frame.iloc[pattern_start + 1] = [91.3, 91.8, 91.0, 91.5, 900.0]
        frame.iloc[pattern_start + 2] = [91.7, 96.5, 91.5, 96.2, 1300.0]
        if with_confirmation:
            frame.iloc[-1] = [96.2, 97.2, 96.0, 97.0, 1400.0]
        return frame

    def test_morning_star_after_downtrend_is_reported_as_pending(self):
        frame = self._set_morning_star(candle_frame("down"))
        analysis = analyze_market_patterns(frame, [])
        pattern = next(p for p in analysis["candlestick_patterns"] if p["name_en"] == "Morning Star")
        self.assertEqual(pattern["state"], "NEEDS_CONFIRMATION")
        self.assertEqual(pattern["direction"], "NEUTRAL")
        self.assertEqual(pattern["potential_direction"], "BULLISH")
        self.assertEqual(pattern["name_fa"], "ستاره صبحگاهی")

    def test_morning_star_requires_follow_through_close_above_third_high(self):
        frame = self._set_morning_star(candle_frame("down"), with_confirmation=True)
        analysis = analyze_market_patterns(frame, [])
        pattern = next(p for p in analysis["candlestick_patterns"] if p["name_en"] == "Morning Star")
        self.assertEqual(pattern["state"], "CONFIRMED")
        self.assertEqual(pattern["direction"], "BULLISH")
        self.assertGreater(pattern["score"], 0)

    def test_morning_star_is_not_detected_without_prior_downtrend(self):
        frame = self._set_morning_star(candle_frame("up"))
        analysis = analyze_market_patterns(frame, [])
        self.assertFalse(any(p["name_en"] == "Morning Star" for p in analysis["candlestick_patterns"]))

    def _set_three_white_soldiers(self, frame: pd.DataFrame) -> pd.DataFrame:
        frame.iloc[-3] = [95.0, 101.0, 94.8, 100.8, 1200.0]
        frame.iloc[-2] = [98.0, 104.2, 97.8, 104.0, 1300.0]
        frame.iloc[-1] = [101.0, 110.2, 100.8, 110.0, 1500.0]
        return frame

    def test_three_white_soldiers_requires_three_strong_green_candles_and_prior_context(self):
        frame = self._set_three_white_soldiers(candle_frame("down"))
        analysis = analyze_market_patterns(frame, [])
        pattern = next(p for p in analysis["candlestick_patterns"] if p["name_en"] == "Three White Soldiers")
        self.assertEqual(pattern["direction"], "BULLISH")
        self.assertEqual(pattern["state"], "NEEDS_CONFIRMATION")
        self.assertEqual(pattern["name_fa"], "سه سرباز سفید")

    def test_three_white_soldiers_rejected_when_opens_are_not_inside_previous_bodies(self):
        frame = self._set_three_white_soldiers(candle_frame("down"))
        frame.iloc[-2, frame.columns.get_loc("open")] = 90.0
        analysis = analyze_market_patterns(frame, [])
        self.assertFalse(any(p["name_en"] == "Three White Soldiers" for p in analysis["candlestick_patterns"]))

    def test_three_white_soldiers_rejected_after_strong_existing_uptrend(self):
        frame = self._set_three_white_soldiers(candle_frame("up"))
        analysis = analyze_market_patterns(frame, [])
        self.assertFalse(any(p["name_en"] == "Three White Soldiers" for p in analysis["candlestick_patterns"]))

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


if __name__ == "__main__":
    unittest.main()
