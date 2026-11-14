from __future__ import annotations

import unittest
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from unittest.mock import patch

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

    def _set_evening_star(self, frame: pd.DataFrame, *, with_confirmation: bool = False) -> pd.DataFrame:
        pattern_start = -4 if with_confirmation else -3
        frame.iloc[pattern_start] = [100.0, 110.0, 99.0, 109.0, 1500.0]
        frame.iloc[pattern_start + 1] = [109.2, 110.0, 108.5, 109.5, 900.0]
        frame.iloc[pattern_start + 2] = [109.0, 110.0, 102.0, 104.0, 1800.0]
        if with_confirmation:
            frame.iloc[-1] = [103.0, 104.0, 100.0, 101.0, 1900.0]
        return frame

    def test_evening_star_after_uptrend_is_pending_bearish_evidence(self):
        frame = self._set_evening_star(candle_frame("up"))
        analysis = analyze_market_patterns(frame, [])
        pattern = next(p for p in analysis["candlestick_patterns"] if p["name_en"] == "Evening Star")
        self.assertEqual(pattern["name_fa"], "ستاره شامگاهی")
        self.assertEqual(pattern["direction"], "NEUTRAL")
        self.assertEqual(pattern["potential_direction"], "BEARISH")
        self.assertEqual(pattern["state"], "NEEDS_CONFIRMATION")
        self.assertEqual(pattern["trigger_price"], 102.0)
        self.assertEqual(pattern["invalidation_price"], 110.0)

    def test_evening_star_confirmed_by_next_close_below_third_low(self):
        frame = self._set_evening_star(candle_frame("up"), with_confirmation=True)
        analysis = analyze_market_patterns(frame, [])
        pattern = next(p for p in analysis["candlestick_patterns"] if p["name_en"] == "Evening Star")
        self.assertEqual(pattern["state"], "CONFIRMED")
        self.assertEqual(pattern["direction"], "BEARISH")
        self.assertIsNone(pattern["potential_direction"])

    def test_evening_star_rejected_without_prior_uptrend(self):
        frame = self._set_evening_star(candle_frame("down"))
        analysis = analyze_market_patterns(frame, [])
        self.assertFalse(any(p["name_en"] == "Evening Star" for p in analysis["candlestick_patterns"]))

    def test_evening_star_rejected_when_third_close_does_not_reach_first_body_midpoint(self):
        frame = self._set_evening_star(candle_frame("up"))
        frame.iloc[-1] = [109.0, 110.0, 105.0, 106.0, 1800.0]
        analysis = analyze_market_patterns(frame, [])
        self.assertFalse(any(p["name_en"] == "Evening Star" for p in analysis["candlestick_patterns"]))

    def test_evening_star_rejected_when_middle_body_is_not_small(self):
        frame = self._set_evening_star(candle_frame("up"))
        frame.iloc[-2] = [108.0, 112.0, 107.5, 111.0, 900.0]
        analysis = analyze_market_patterns(frame, [])
        self.assertFalse(any(p["name_en"] == "Evening Star" for p in analysis["candlestick_patterns"]))

    def _set_three_black_crows(self, frame: pd.DataFrame, *, with_confirmation: bool = False) -> pd.DataFrame:
        pattern_start = -4 if with_confirmation else -3
        frame.iloc[pattern_start] = [110.0, 110.2, 103.8, 104.5, 1500.0]
        frame.iloc[pattern_start + 1] = [109.0, 109.2, 102.8, 103.0, 1600.0]
        frame.iloc[pattern_start + 2] = [108.0, 108.2, 101.8, 102.0, 1700.0]
        if with_confirmation:
            frame.iloc[-1] = [102.0, 102.1, 100.8, 101.0, 1900.0]
        return frame

    def test_three_black_crows_after_uptrend_is_pending_bearish_evidence(self):
        frame = self._set_three_black_crows(candle_frame("up"))
        analysis = analyze_market_patterns(frame, [])
        pattern = next(p for p in analysis["candlestick_patterns"] if p["name_en"] == "Three Black Crows")
        self.assertEqual(pattern["name_fa"], "سه کلاغ سیاه")
        self.assertEqual(pattern["direction"], "NEUTRAL")
        self.assertEqual(pattern["potential_direction"], "BEARISH")
        self.assertEqual(pattern["state"], "NEEDS_CONFIRMATION")
        self.assertEqual(pattern["trigger_price"], 101.8)
        self.assertEqual(pattern["invalidation_price"], 110.2)

    def test_three_black_crows_confirmed_by_next_close_below_third_low(self):
        frame = self._set_three_black_crows(candle_frame("up"), with_confirmation=True)
        analysis = analyze_market_patterns(frame, [])
        pattern = next(p for p in analysis["candlestick_patterns"] if p["name_en"] == "Three Black Crows")
        self.assertEqual(pattern["state"], "CONFIRMED")
        self.assertEqual(pattern["direction"], "BEARISH")
        self.assertIsNone(pattern["potential_direction"])

    def test_three_black_crows_rejected_without_prior_uptrend(self):
        frame = self._set_three_black_crows(candle_frame("down"))
        analysis = analyze_market_patterns(frame, [])
        self.assertFalse(any(p["name_en"] == "Three Black Crows" for p in analysis["candlestick_patterns"]))

    def test_three_black_crows_rejected_when_second_candle_opens_outside_first_body(self):
        frame = self._set_three_black_crows(candle_frame("up"))
        frame.iloc[-2] = [112.0, 112.2, 102.8, 103.0, 1600.0]
        analysis = analyze_market_patterns(frame, [])
        self.assertFalse(any(p["name_en"] == "Three Black Crows" for p in analysis["candlestick_patterns"]))

    def test_three_black_crows_rejected_when_upper_shadow_is_too_long(self):
        frame = self._set_three_black_crows(candle_frame("up"))
        frame.iloc[-1] = [108.0, 110.0, 101.8, 102.0, 1700.0]
        analysis = analyze_market_patterns(frame, [])
        self.assertFalse(any(p["name_en"] == "Three Black Crows" for p in analysis["candlestick_patterns"]))

    def _set_shooting_star(
        self, frame: pd.DataFrame, *, with_confirmation: bool = False, bearish_body: bool = False
    ) -> pd.DataFrame:
        index = -2 if with_confirmation else -1
        base = float(frame.iloc[index - 1]["close"])
        if bearish_body:
            frame.iloc[index] = [base + 1.0, base + 6.1, base - 0.1, base, 1600.0]
        else:
            frame.iloc[index] = [base, base + 6.1, base - 0.1, base + 1.0, 1600.0]
        if with_confirmation:
            pattern_low = base - 0.1
            frame.iloc[-1] = [pattern_low + 0.05, pattern_low + 0.08,
                              pattern_low - 1.0, pattern_low - 0.5, 1900.0]
        return frame

    def test_shooting_star_after_uptrend_is_pending_bearish_evidence(self):
        frame = self._set_shooting_star(candle_frame("up"))
        analysis = analyze_market_patterns(frame, [])
        pattern = next(p for p in analysis["candlestick_patterns"] if p["name_en"] == "Shooting Star")
        self.assertEqual(pattern["name_fa"], "ستاره ثاقب")
        self.assertEqual(pattern["direction"], "NEUTRAL")
        self.assertEqual(pattern["potential_direction"], "BEARISH")
        self.assertEqual(pattern["state"], "NEEDS_CONFIRMATION")
        self.assertAlmostEqual(pattern["trigger_price"], frame.iloc[-1]["low"])
        self.assertAlmostEqual(pattern["invalidation_price"], frame.iloc[-1]["high"])

    def test_shooting_star_confirmed_by_next_close_below_pattern_low(self):
        frame = self._set_shooting_star(candle_frame("up"), with_confirmation=True)
        analysis = analyze_market_patterns(frame, [])
        pattern = next(p for p in analysis["candlestick_patterns"] if p["name_en"] == "Shooting Star")
        self.assertEqual(pattern["state"], "CONFIRMED")
        self.assertEqual(pattern["direction"], "BEARISH")
        self.assertIsNone(pattern["potential_direction"])

    def test_shooting_star_accepts_bearish_body_and_scores_it_more_strongly(self):
        bullish = self._set_shooting_star(candle_frame("up"))
        bearish = self._set_shooting_star(candle_frame("up"), bearish_body=True)
        bullish_pattern = next(
            p for p in analyze_market_patterns(bullish, [])["candlestick_patterns"]
            if p["name_en"] == "Shooting Star"
        )
        bearish_pattern = next(
            p for p in analyze_market_patterns(bearish, [])["candlestick_patterns"]
            if p["name_en"] == "Shooting Star"
        )
        self.assertLess(bearish_pattern["score"], bullish_pattern["score"])

    def test_shooting_star_rejected_without_prior_uptrend(self):
        frame = self._set_shooting_star(candle_frame("down"))
        analysis = analyze_market_patterns(frame, [])
        self.assertFalse(any(p["name_en"] == "Shooting Star" for p in analysis["candlestick_patterns"]))

    def test_shooting_star_rejected_when_upper_shadow_is_too_short(self):
        frame = self._set_shooting_star(candle_frame("up"))
        base = float(frame.iloc[-2]["close"])
        frame.iloc[-1] = [base, base + 1.2, base - 0.1, base + 1.0, 1600.0]
        analysis = analyze_market_patterns(frame, [])
        self.assertFalse(any(p["name_en"] == "Shooting Star" for p in analysis["candlestick_patterns"]))

    def test_shooting_star_rejected_when_lower_shadow_is_too_long(self):
        frame = self._set_shooting_star(candle_frame("up"))
        base = float(frame.iloc[-2]["close"])
        frame.iloc[-1] = [base, base + 6.1, base - 1.5, base + 1.0, 1600.0]
        analysis = analyze_market_patterns(frame, [])
        self.assertFalse(any(p["name_en"] == "Shooting Star" for p in analysis["candlestick_patterns"]))

    def _set_hanging_man(
        self, frame: pd.DataFrame, *, with_confirmation: bool = False, bearish_body: bool = False
    ) -> pd.DataFrame:
        index = -2 if with_confirmation else -1
        base = float(frame.iloc[index - 1]["close"])
        if bearish_body:
            frame.iloc[index] = [base + 1.0, base + 1.1, base - 5.1, base, 1600.0]
        else:
            frame.iloc[index] = [base, base + 1.1, base - 5.1, base + 1.0, 1600.0]
        if with_confirmation:
            pattern_low = base - 5.1
            frame.iloc[-1] = [pattern_low + 0.2, pattern_low + 0.3, pattern_low - 1.0,
                              pattern_low - 0.5, 1900.0]
        return frame

    def test_hanging_man_after_uptrend_is_pending_bearish_evidence(self):
        frame = self._set_hanging_man(candle_frame("up"))
        analysis = analyze_market_patterns(frame, [])
        pattern = next(p for p in analysis["candlestick_patterns"] if p["name_en"] == "Hanging Man")
        self.assertEqual(pattern["name_fa"], "مرد آویزان")
        self.assertEqual(pattern["direction"], "NEUTRAL")
        self.assertEqual(pattern["potential_direction"], "BEARISH")
        self.assertEqual(pattern["state"], "NEEDS_CONFIRMATION")
        self.assertEqual(pattern["trigger_price"], frame.iloc[-1]["low"])
        self.assertAlmostEqual(pattern["invalidation_price"], frame.iloc[-1]["high"])

    def test_hanging_man_confirmed_by_next_close_below_pattern_low(self):
        frame = self._set_hanging_man(candle_frame("up"), with_confirmation=True)
        analysis = analyze_market_patterns(frame, [])
        pattern = next(p for p in analysis["candlestick_patterns"] if p["name_en"] == "Hanging Man")
        self.assertEqual(pattern["state"], "CONFIRMED")
        self.assertEqual(pattern["direction"], "BEARISH")
        self.assertIsNone(pattern["potential_direction"])

    def test_hanging_man_accepts_bearish_body_and_scores_it_more_strongly(self):
        bullish = self._set_hanging_man(candle_frame("up"))
        bearish = self._set_hanging_man(candle_frame("up"), bearish_body=True)
        bullish_pattern = next(
            p for p in analyze_market_patterns(bullish, [])["candlestick_patterns"]
            if p["name_en"] == "Hanging Man"
        )
        bearish_pattern = next(
            p for p in analyze_market_patterns(bearish, [])["candlestick_patterns"]
            if p["name_en"] == "Hanging Man"
        )
        self.assertLess(bearish_pattern["score"], bullish_pattern["score"])

    def test_hanging_man_rejected_without_prior_uptrend(self):
        frame = self._set_hanging_man(candle_frame("down"))
        analysis = analyze_market_patterns(frame, [])
        self.assertFalse(any(p["name_en"] == "Hanging Man" for p in analysis["candlestick_patterns"]))

    def test_hanging_man_rejected_when_lower_shadow_is_too_short(self):
        frame = self._set_hanging_man(candle_frame("up"))
        base = float(frame.iloc[-2]["close"])
        frame.iloc[-1] = [base, base + 1.1, base - 1.0, base + 1.0, 1600.0]
        analysis = analyze_market_patterns(frame, [])
        self.assertFalse(any(p["name_en"] == "Hanging Man" for p in analysis["candlestick_patterns"]))

    def test_hanging_man_rejected_when_upper_shadow_is_too_long(self):
        frame = self._set_hanging_man(candle_frame("up"))
        base = float(frame.iloc[-2]["close"])
        frame.iloc[-1] = [base, base + 2.0, base - 5.1, base + 1.0, 1600.0]
        analysis = analyze_market_patterns(frame, [])
        self.assertFalse(any(p["name_en"] == "Hanging Man" for p in analysis["candlestick_patterns"]))

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

    def _set_bearish_engulfing(self, frame: pd.DataFrame, *, with_confirmation: bool = False) -> pd.DataFrame:
        start = -3 if with_confirmation else -2
        frame.iloc[start] = [100.0, 101.0, 99.5, 100.8, 1200.0]
        frame.iloc[start + 1] = [101.0, 101.5, 98.8, 99.0, 1800.0]
        if with_confirmation:
            frame.iloc[-1] = [98.9, 99.0, 97.5, 98.0, 1900.0]
        return frame

    def test_bearish_engulfing_after_uptrend_is_pending_bearish_evidence(self):
        frame = self._set_bearish_engulfing(candle_frame("up"))
        analysis = analyze_market_patterns(frame, [])
        pattern = next(p for p in analysis["candlestick_patterns"] if p["name_en"] == "Bearish Engulfing")
        self.assertEqual(pattern["name_fa"], "پوشای نزولی")
        self.assertEqual(pattern["direction"], "NEUTRAL")
        self.assertEqual(pattern["potential_direction"], "BEARISH")
        self.assertEqual(pattern["state"], "NEEDS_CONFIRMATION")
        self.assertEqual(pattern["trigger_price"], 98.8)
        self.assertEqual(pattern["invalidation_price"], 101.5)

    def test_bearish_engulfing_confirmed_by_next_close_below_engulfing_low(self):
        frame = self._set_bearish_engulfing(candle_frame("up"), with_confirmation=True)
        analysis = analyze_market_patterns(frame, [])
        pattern = next(p for p in analysis["candlestick_patterns"] if p["name_en"] == "Bearish Engulfing")
        self.assertEqual(pattern["state"], "CONFIRMED")
        self.assertEqual(pattern["direction"], "BEARISH")
        self.assertIsNone(pattern["potential_direction"])

    def test_bearish_engulfing_rejected_without_prior_uptrend(self):
        frame = self._set_bearish_engulfing(candle_frame("down"))
        analysis = analyze_market_patterns(frame, [])
        self.assertFalse(any(p["name_en"] == "Bearish Engulfing" for p in analysis["candlestick_patterns"]))

    def test_bearish_engulfing_rejected_when_second_body_does_not_engulf_first(self):
        frame = self._set_bearish_engulfing(candle_frame("up"))
        frame.iloc[-1] = [100.7, 101.0, 99.0, 100.0, 1800.0]
        analysis = analyze_market_patterns(frame, [])
        self.assertFalse(any(p["name_en"] == "Bearish Engulfing" for p in analysis["candlestick_patterns"]))

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
        frame = candle_frame("down")
        frame.iloc[-2] = [96.0, 96.1, 92.0, 96.0, 1500.0]
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
        frame.iloc[-1] = [96.0, 96.1, 95.9, 96.0, 1500.0]
        analysis = analyze_market_patterns(frame, [])
        self.assertFalse(any(p["name_en"] == "Dragonfly Doji" for p in analysis["candlestick_patterns"]))

    def _set_piercing_line(self, frame: pd.DataFrame, *, with_confirmation: bool = False) -> pd.DataFrame:
        start = -3 if with_confirmation else -2
        frame.iloc[start] = [100.0, 100.5, 90.0, 91.0, 1500.0]
        frame.iloc[start + 1] = [89.5, 97.0, 89.0, 96.5, 1800.0]
        if with_confirmation:
            frame.iloc[-1] = [96.5, 99.0, 96.2, 98.0, 1900.0]
        return frame

    def test_piercing_line_after_downtrend_is_pending_bullish_evidence(self):
        frame = self._set_piercing_line(candle_frame("down"))
        analysis = analyze_market_patterns(frame, [])
        pattern = next(p for p in analysis["candlestick_patterns"] if p["name_en"] == "Piercing Line")
        self.assertEqual(pattern["name_fa"], "خط نفوذی صعودی")
        self.assertEqual(pattern["direction"], "NEUTRAL")
        self.assertEqual(pattern["potential_direction"], "BULLISH")
        self.assertEqual(pattern["state"], "NEEDS_CONFIRMATION")
        self.assertEqual(pattern["trigger_price"], 97.0)
        self.assertEqual(pattern["invalidation_price"], 89.0)

    def test_piercing_line_confirmed_by_next_close_above_second_high(self):
        frame = self._set_piercing_line(candle_frame("down"), with_confirmation=True)
        analysis = analyze_market_patterns(frame, [])
        pattern = next(p for p in analysis["candlestick_patterns"] if p["name_en"] == "Piercing Line")
        self.assertEqual(pattern["state"], "CONFIRMED")
        self.assertEqual(pattern["direction"], "BULLISH")
        self.assertIsNone(pattern["potential_direction"])

    def test_piercing_line_rejected_without_prior_downtrend(self):
        frame = self._set_piercing_line(candle_frame("up"))
        analysis = analyze_market_patterns(frame, [])
        self.assertFalse(any(p["name_en"] == "Piercing Line" for p in analysis["candlestick_patterns"]))

    def test_piercing_line_rejected_when_second_close_does_not_cross_first_body_midpoint(self):
        frame = self._set_piercing_line(candle_frame("down"))
        frame.iloc[-1, frame.columns.get_loc("close")] = 94.0
        analysis = analyze_market_patterns(frame, [])
        self.assertFalse(any(p["name_en"] == "Piercing Line" for p in analysis["candlestick_patterns"]))

    def test_piercing_line_rejected_when_second_candle_does_not_open_below_first_low(self):
        frame = self._set_piercing_line(candle_frame("down"))
        frame.iloc[-1, frame.columns.get_loc("open")] = 90.5
        analysis = analyze_market_patterns(frame, [])
        self.assertFalse(any(p["name_en"] == "Piercing Line" for p in analysis["candlestick_patterns"]))


    def _set_bullish_marubozu(self, frame: pd.DataFrame, *, with_confirmation: bool = False) -> pd.DataFrame:
        index = -2 if with_confirmation else -1
        frame.iloc[index] = [96.0, 101.0, 96.0, 101.0, 1800.0]
        if with_confirmation:
            frame.iloc[-1] = [101.0, 103.0, 100.8, 102.5, 1900.0]
        return frame

    def test_bullish_marubozu_detected_after_downtrend_as_pending_evidence(self):
        frame = self._set_bullish_marubozu(candle_frame("down"))
        analysis = analyze_market_patterns(frame, [])
        pattern = next(p for p in analysis["candlestick_patterns"] if p["name_en"] == "Bullish Marubozu")
        self.assertEqual(pattern["name_fa"], "ماروبوزوی صعودی")
        self.assertEqual(pattern["direction"], "NEUTRAL")
        self.assertEqual(pattern["potential_direction"], "BULLISH")
        self.assertEqual(pattern["state"], "NEEDS_CONFIRMATION")
        self.assertEqual(pattern["trigger_price"], 101.0)
        self.assertEqual(pattern["invalidation_price"], 96.0)

    def test_bullish_marubozu_confirmed_by_next_close_above_high(self):
        frame = self._set_bullish_marubozu(candle_frame("down"), with_confirmation=True)
        analysis = analyze_market_patterns(frame, [])
        pattern = next(p for p in analysis["candlestick_patterns"] if p["name_en"] == "Bullish Marubozu")
        self.assertEqual(pattern["state"], "CONFIRMED")
        self.assertEqual(pattern["direction"], "BULLISH")
        self.assertIsNone(pattern["potential_direction"])

    def test_bullish_marubozu_rejected_when_shadows_are_too_long(self):
        frame = self._set_bullish_marubozu(candle_frame("down"))
        frame.iloc[-1] = [96.0, 102.0, 95.0, 100.0, 1800.0]
        analysis = analyze_market_patterns(frame, [])
        self.assertFalse(any(p["name_en"] == "Bullish Marubozu" for p in analysis["candlestick_patterns"]))

    def test_bullish_marubozu_rejected_when_body_is_not_long_relative_to_prior_candles(self):
        frame = self._set_bullish_marubozu(candle_frame("down"))
        # Make the preceding bodies larger while preserving the prior downtrend.
        # A 0.45 body must not pass the 1.5x median-body requirement when the
        # preceding median body is 0.50.
        prior = frame.iloc[-9:-1]
        frame.iloc[-9:-1, frame.columns.get_loc("open")] = prior["close"].to_numpy() + 0.50
        frame.iloc[-9:-1, frame.columns.get_loc("high")] = prior["close"].to_numpy() + 0.55
        frame.iloc[-9:-1, frame.columns.get_loc("low")] = prior["close"].to_numpy() - 0.05
        frame.iloc[-1] = [96.0, 96.45, 96.0, 96.45, 1800.0]
        analysis = analyze_market_patterns(frame, [])
        self.assertFalse(any(p["name_en"] == "Bullish Marubozu" for p in analysis["candlestick_patterns"]))

    def test_bullish_marubozu_rejected_for_bearish_or_doji_candle(self):
        frame = self._set_bullish_marubozu(candle_frame("down"))
        frame.iloc[-1] = [101.0, 101.0, 96.0, 96.0, 1800.0]
        analysis = analyze_market_patterns(frame, [])
        self.assertFalse(any(p["name_en"] == "Bullish Marubozu" for p in analysis["candlestick_patterns"]))


    def _set_bullish_abandoned_baby(self, frame: pd.DataFrame, *, with_confirmation: bool = False) -> pd.DataFrame:
        start = -4 if with_confirmation else -3
        frame.iloc[start] = [100.0, 101.0, 93.0, 94.0, 1500.0]
        frame.iloc[start + 1] = [91.0, 92.0, 90.0, 91.0, 900.0]
        frame.iloc[start + 2] = [94.0, 100.0, 93.0, 99.0, 1800.0]
        if with_confirmation:
            frame.iloc[-1] = [99.0, 101.0, 98.8, 100.5, 1900.0]
        return frame

    def test_bullish_abandoned_baby_requires_both_full_range_gaps(self):
        frame = self._set_bullish_abandoned_baby(candle_frame("down"))
        analysis = analyze_market_patterns(frame, [])
        pattern = next(p for p in analysis["candlestick_patterns"] if p["name_en"] == "Bullish Abandoned Baby")
        self.assertEqual(pattern["name_fa"], "نوزاد رهاشدهٔ صعودی")
        self.assertEqual(pattern["direction"], "NEUTRAL")
        self.assertEqual(pattern["potential_direction"], "BULLISH")
        self.assertEqual(pattern["state"], "NEEDS_CONFIRMATION")
        self.assertEqual(pattern["trigger_price"], 100.0)
        self.assertEqual(pattern["invalidation_price"], 93.0)

    def test_bullish_abandoned_baby_confirmed_by_follow_through_close(self):
        frame = self._set_bullish_abandoned_baby(candle_frame("down"), with_confirmation=True)
        analysis = analyze_market_patterns(frame, [])
        pattern = next(p for p in analysis["candlestick_patterns"] if p["name_en"] == "Bullish Abandoned Baby")
        self.assertEqual(pattern["state"], "CONFIRMED")
        self.assertEqual(pattern["direction"], "BULLISH")
        self.assertIsNone(pattern["potential_direction"])

    def test_bullish_abandoned_baby_rejected_when_doji_overlaps_first_candle(self):
        frame = self._set_bullish_abandoned_baby(candle_frame("down"))
        frame.iloc[-2] = [93.5, 94.0, 92.5, 93.6, 900.0]
        analysis = analyze_market_patterns(frame, [])
        self.assertFalse(any(p["name_en"] == "Bullish Abandoned Baby" for p in analysis["candlestick_patterns"]))

    def test_bullish_abandoned_baby_rejected_when_third_candle_overlaps_doji(self):
        frame = self._set_bullish_abandoned_baby(candle_frame("down"))
        frame.iloc[-1] = [91.5, 100.0, 91.0, 99.0, 1800.0]
        analysis = analyze_market_patterns(frame, [])
        self.assertFalse(any(p["name_en"] == "Bullish Abandoned Baby" for p in analysis["candlestick_patterns"]))

    def test_bullish_abandoned_baby_rejected_without_prior_downtrend(self):
        frame = self._set_bullish_abandoned_baby(candle_frame("up"))
        analysis = analyze_market_patterns(frame, [])
        self.assertFalse(any(p["name_en"] == "Bullish Abandoned Baby" for p in analysis["candlestick_patterns"]))

    def test_bullish_abandoned_baby_rejected_when_middle_candle_is_not_doji(self):
        frame = self._set_bullish_abandoned_baby(candle_frame("down"))
        frame.iloc[-2] = [91.0, 92.0, 90.0, 91.8, 900.0]
        analysis = analyze_market_patterns(frame, [])
        self.assertFalse(any(p["name_en"] == "Bullish Abandoned Baby" for p in analysis["candlestick_patterns"]))


    def _set_rising_window(self, frame: pd.DataFrame, *, with_follow_through: bool = False) -> pd.DataFrame:
        first_index = -3 if with_follow_through else -2
        frame.iloc[first_index] = [100.0, 102.0, 99.5, 101.0, 1400.0]
        frame.iloc[first_index + 1] = [103.0, 106.0, 102.5, 105.0, 1800.0]
        if with_follow_through:
            frame.iloc[-1] = [105.2, 108.0, 104.0, 107.5, 1900.0]
        return frame

    def test_rising_window_requires_a_full_non_overlapping_gap_after_uptrend(self):
        frame = self._set_rising_window(candle_frame("up"))
        analysis = analyze_market_patterns(frame, [])
        pattern = next(p for p in analysis["candlestick_patterns"] if p["name_en"] == "Rising Window")
        self.assertEqual(pattern["name_fa"], "پنجرهٔ صعودی (گپ رو به بالا)")
        self.assertEqual(pattern["direction"], "NEUTRAL")
        self.assertEqual(pattern["potential_direction"], "BULLISH")
        self.assertEqual(pattern["state"], "NEEDS_CONFIRMATION")
        self.assertEqual(pattern["trigger_price"], 106.0)
        self.assertEqual(pattern["invalidation_price"], 102.5)

    def test_rising_window_is_confirmed_while_following_candles_stay_above_gap(self):
        frame = self._set_rising_window(candle_frame("up"), with_follow_through=True)
        analysis = analyze_market_patterns(frame, [])
        pattern = next(p for p in analysis["candlestick_patterns"] if p["name_en"] == "Rising Window")
        self.assertEqual(pattern["state"], "CONFIRMED")
        self.assertEqual(pattern["direction"], "BULLISH")
        self.assertIsNone(pattern["potential_direction"])

    def test_rising_window_rejected_when_second_candle_overlaps_first_high(self):
        frame = self._set_rising_window(candle_frame("up"))
        frame.iloc[-1] = [103.0, 106.0, 101.8, 105.0, 1800.0]
        analysis = analyze_market_patterns(frame, [])
        self.assertFalse(any(p["name_en"] == "Rising Window" for p in analysis["candlestick_patterns"]))

    def test_rising_window_rejected_when_prior_trend_is_not_bullish(self):
        frame = self._set_rising_window(candle_frame("down"))
        analysis = analyze_market_patterns(frame, [])
        self.assertFalse(any(p["name_en"] == "Rising Window" for p in analysis["candlestick_patterns"]))

    def test_rising_window_invalidated_when_a_later_candle_enters_gap(self):
        frame = self._set_rising_window(candle_frame("up"), with_follow_through=True)
        frame.iloc[-1] = [105.2, 108.0, 101.9, 107.5, 1900.0]
        analysis = analyze_market_patterns(frame, [])
        self.assertFalse(any(p["name_en"] == "Rising Window" for p in analysis["candlestick_patterns"]))




    # Preserve regression coverage for the existing ADX/EMA200/divergence/Fibonacci outputs.
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


if __name__ == "__main__":
    unittest.main()
