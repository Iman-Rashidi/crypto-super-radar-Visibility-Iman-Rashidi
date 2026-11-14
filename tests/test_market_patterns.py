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

    def test_bullish_engulfing_requires_follow_through_confirmation(self):
        frame = candle_frame("down")
        frame.iloc[-2] = [98.0, 98.2, 96.8, 97.0, 1000.0]
        frame.iloc[-1] = [96.7, 99.2, 96.5, 98.5, 1400.0]
        analysis = analyze_market_patterns(frame, [])
        engulfing = next(p for p in analysis["candlestick_patterns"] if p["name_en"] == "Bullish Engulfing")
        self.assertEqual(engulfing["direction"], "NEUTRAL")
        self.assertEqual(engulfing["potential_direction"], "BULLISH")
        self.assertEqual(engulfing["state"], "NEEDS_CONFIRMATION")

    def _set_three_inside_up(self, frame: pd.DataFrame, *, confirmed: bool = True) -> pd.DataFrame:
        frame.iloc[-3] = [100.0, 101.0, 93.0, 94.0, 1400.0]
        frame.iloc[-2] = [95.0, 97.0, 94.5, 96.0, 1100.0]
        frame.iloc[-1] = [96.2, 103.0 if confirmed else 99.0, 95.8, 102.0 if confirmed else 98.5, 1600.0]
        return frame

    def test_three_inside_up_confirmed_when_third_closes_above_first_high(self):
        frame = self._set_three_inside_up(candle_frame("down"))
        analysis = analyze_market_patterns(frame, [])
        pattern = next(p for p in analysis["candlestick_patterns"] if p["name_en"] == "Three Inside Up")
        self.assertEqual(pattern["direction"], "BULLISH")
        self.assertEqual(pattern["state"], "CONFIRMED")
        self.assertEqual(pattern["name_fa"], "سه‌تایی از داخل به بالا")

    def test_three_inside_up_waits_when_third_does_not_close_above_first_high(self):
        frame = self._set_three_inside_up(candle_frame("down"), confirmed=False)
        analysis = analyze_market_patterns(frame, [])
        pattern = next(p for p in analysis["candlestick_patterns"] if p["name_en"] == "Three Inside Up")
        self.assertEqual(pattern["direction"], "NEUTRAL")
        self.assertEqual(pattern["potential_direction"], "BULLISH")
        self.assertEqual(pattern["state"], "NEEDS_CONFIRMATION")

    def test_three_inside_up_rejected_without_prior_downtrend(self):
        frame = self._set_three_inside_up(candle_frame("up"))
        analysis = analyze_market_patterns(frame, [])
        self.assertFalse(any(p["name_en"] == "Three Inside Up" for p in analysis["candlestick_patterns"]))

    def test_three_inside_up_rejected_when_harami_candle_is_outside_first_candle(self):
        frame = self._set_three_inside_up(candle_frame("down"))
        frame.iloc[-2, frame.columns.get_loc("close")] = 102.0
        analysis = analyze_market_patterns(frame, [])
        self.assertFalse(any(p["name_en"] == "Three Inside Up" for p in analysis["candlestick_patterns"]))

    def _set_three_line_strike(self, frame: pd.DataFrame, *, with_confirmation: bool = False) -> pd.DataFrame:
        start = -5 if with_confirmation else -4
        frame.iloc[start] = [95.0, 99.2, 94.8, 99.0, 1100.0]
        frame.iloc[start + 1] = [98.0, 103.2, 97.8, 103.0, 1200.0]
        frame.iloc[start + 2] = [102.0, 108.2, 101.8, 108.0, 1300.0]
        frame.iloc[start + 3] = [109.0, 109.2, 94.0, 94.5, 1800.0]
        if with_confirmation:
            frame.iloc[-1] = [94.5, 111.0, 94.2, 110.0, 2000.0]
        return frame

    def test_bullish_three_line_strike_detected_as_continuation_setup(self):
        frame = self._set_three_line_strike(candle_frame("up"))
        # Establish rising context before the four-candle pattern.
        frame.iloc[-12:-4, frame.columns.get_loc("close")] = np.linspace(85.0, 94.0, 8)
        analysis = analyze_market_patterns(frame, [])
        pattern = next(p for p in analysis["candlestick_patterns"] if p["name_en"] == "Bullish Three Line Strike")
        self.assertEqual(pattern["direction"], "NEUTRAL")
        self.assertEqual(pattern["potential_direction"], "BULLISH")
        self.assertEqual(pattern["state"], "NEEDS_CONFIRMATION")
        self.assertEqual(pattern["name_fa"], "ضربهٔ سه‌خطی صعودی")

    def test_bullish_three_line_strike_confirmed_by_close_above_fourth_high(self):
        frame = self._set_three_line_strike(candle_frame("up"), with_confirmation=True)
        frame.iloc[-13:-5, frame.columns.get_loc("close")] = np.linspace(85.0, 94.0, 8)
        analysis = analyze_market_patterns(frame, [])
        pattern = next(p for p in analysis["candlestick_patterns"] if p["name_en"] == "Bullish Three Line Strike")
        self.assertEqual(pattern["direction"], "BULLISH")
        self.assertEqual(pattern["state"], "CONFIRMED")

    def test_bullish_three_line_strike_rejected_without_prior_uptrend(self):
        frame = self._set_three_line_strike(candle_frame("flat"))
        analysis = analyze_market_patterns(frame, [])
        self.assertFalse(any(p["name_en"] == "Bullish Three Line Strike" for p in analysis["candlestick_patterns"]))

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


    def _set_dragonfly_doji(self, frame: pd.DataFrame) -> pd.DataFrame:
        frame.iloc[-1] = [96.0, 96.1, 92.0, 96.0, 1500.0]
        return frame

    def test_dragonfly_doji_after_downtrend_is_pending_bullish_evidence(self):
        frame = self._set_dragonfly_doji(candle_frame("down"))
        analysis = analyze_market_patterns(frame, [])
        pattern = next(p for p in analysis["candlestick_patterns"] if p["name_en"] == "Dragonfly Doji")
        self.assertEqual(pattern["name_fa"], "دوجی سنجاقک")
        self.assertEqual(pattern["direction"], "NEUTRAL")
        self.assertEqual(pattern["potential_direction"], "BULLISH")
        self.assertEqual(pattern["state"], "NEEDS_CONFIRMATION")
        self.assertEqual(pattern["timestamp"], frame.index[-1].isoformat())
        self.assertEqual(pattern["trigger_price"], 96.1)
        self.assertEqual(pattern["invalidation_price"], 92.0)

    def test_dragonfly_doji_confirmed_by_next_close_above_its_high(self):
        frame = self._set_dragonfly_doji(candle_frame("down"))
        frame.iloc[-1] = [96.0, 97.0, 95.8, 96.8, 1600.0]
        analysis = analyze_market_patterns(frame, [])
        pattern = next(p for p in analysis["candlestick_patterns"] if p["name_en"] == "Dragonfly Doji")
        self.assertEqual(pattern["state"], "CONFIRMED")
        self.assertEqual(pattern["direction"], "BULLISH")
        self.assertIsNone(pattern["potential_direction"])

    def test_dragonfly_doji_rejected_when_upper_shadow_is_too_long(self):
        frame = self._set_dragonfly_doji(candle_frame("down"))
        frame.iloc[-1] = [95.0, 98.0, 92.0, 95.0, 1500.0]
        analysis = analyze_market_patterns(frame, [])
        self.assertFalse(any(p["name_en"] == "Dragonfly Doji" for p in analysis["candlestick_patterns"]))

    def test_dragonfly_doji_without_downtrend_has_no_bullish_direction(self):
        frame = self._set_dragonfly_doji(candle_frame("flat"))
        analysis = analyze_market_patterns(frame, [])
        pattern = next(p for p in analysis["candlestick_patterns"] if p["name_en"] == "Dragonfly Doji")
        self.assertEqual(pattern["direction"], "NEUTRAL")
        self.assertIsNone(pattern["potential_direction"])
        self.assertEqual(pattern["score"], 0)

    def test_dragonfly_doji_not_detected_without_long_lower_shadow(self):
        frame = self._set_dragonfly_doji(candle_frame("down"))
        frame.iloc[-1] = [96.0, 96.1, 94.8, 96.0, 1500.0]
        analysis = analyze_market_patterns(frame, [])
        self.assertFalse(any(p["name_en"] == "Dragonfly Doji" for p in analysis["candlestick_patterns"]))


if __name__ == "__main__":
    unittest.main()
