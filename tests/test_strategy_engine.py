from __future__ import annotations

import unittest
from datetime import timedelta

import numpy as np
import pandas as pd

from crypto_super_radar.strategy_engine import (
    MasterStrategyConfig,
    MasterStrategyEngine,
    STRATEGY_VERSION,
)


def make_daily_frame(closes, *, volume=None, highs=None, lows=None):
    closes = np.asarray(closes, dtype=float)
    opens = np.r_[closes[0], closes[:-1]]
    high = np.maximum(opens, closes) + 0.5 if highs is None else np.asarray(highs, dtype=float)
    low = np.minimum(opens, closes) - 0.5 if lows is None else np.asarray(lows, dtype=float)
    volumes = np.full(len(closes), 100.0) if volume is None else np.asarray(volume, dtype=float)
    index = pd.date_range("2025-12-01", periods=len(closes), freq="D", tz="UTC")
    frame = pd.DataFrame({
        "open": opens, "high": high, "low": low,
        "close": closes, "volume": volumes,
    }, index=index)
    return frame


class MasterStrategyEngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = MasterStrategyEngine()
        self.closes_up = 100.0 * np.power(1.003, np.arange(260, dtype=float))

    def test_strong_uptrend_selects_turtle_55_and_builds_atr_levels(self):
        volumes = np.full(260, 100.0)
        volumes[-1] = 180.0
        frame = make_daily_frame(self.closes_up, volume=volumes)
        now = frame.index[-1] + pd.Timedelta(hours=24)

        result = self.engine.evaluate(frame, base_signal="LONG", now=now)

        self.assertEqual(result["version"], STRATEGY_VERSION)
        self.assertEqual(result["status"], "APPROVED")
        self.assertEqual(result["strategy"], "TURTLE_55")
        self.assertEqual(result["direction"], "LONG")
        self.assertLess(result["stop_loss"], result["entry_price"])
        self.assertGreater(result["take_profit"], result["entry_price"])
        self.assertAlmostEqual(result["initial_reward_risk"], 2.0, places=3)
        self.assertGreaterEqual(result["breakout_volume_ratio"], 1.0)

    def test_strong_downtrend_selects_short_turtle_breakout(self):
        closes = 100.0 * np.power(0.997, np.arange(260, dtype=float))
        # Create a genuine close below the prior 55-day Donchian low.
        # A merely downward-trending series with a small daily move is not itself a breakout.
        closes[-1] = float(np.min(closes[-56:-1]) * 0.98)
        volumes = np.full(260, 100.0)
        volumes[-1] = 150.0
        frame = make_daily_frame(closes, volume=volumes)
        now = frame.index[-1] + pd.Timedelta(hours=24)

        result = self.engine.evaluate(frame, base_signal="SHORT", now=now)

        self.assertEqual(result["status"], "APPROVED")
        self.assertEqual(result["direction"], "SHORT")
        self.assertTrue(result["strategy"].startswith("TURTLE_"))
        self.assertGreater(result["stop_loss"], result["entry_price"])
        self.assertLess(result["take_profit"], result["entry_price"])

    def test_strategy_never_overrides_conflicting_twelve_gate_direction(self):
        frame = make_daily_frame(self.closes_up)
        now = frame.index[-1] + pd.Timedelta(hours=24)

        result = self.engine.evaluate(frame, base_signal="SHORT", now=now)

        self.assertEqual(result["status"], "NO_TRADE")
        self.assertEqual(result["reason_code"], "BASE_SIGNAL_CONFLICT")

    def test_sideways_market_returns_no_trade(self):
        values = 100.0 + 1.2 * np.sin(np.arange(260, dtype=float) * 0.35)
        frame = make_daily_frame(values)
        now = frame.index[-1] + pd.Timedelta(hours=24)

        result = self.engine.evaluate(frame, base_signal="LONG", now=now)

        self.assertEqual(result["status"], "NO_TRADE")
        self.assertEqual(result["decision"], "NO_TRADE")
        self.assertIn(result["market_regime"], {"NEUTRAL", "BULLISH", "BEARISH"})

    def test_stale_daily_candles_are_unknown_and_fail_closed(self):
        frame = make_daily_frame(self.closes_up)
        now = frame.index[-1] + pd.Timedelta(hours=72)

        result = self.engine.evaluate(frame, base_signal="LONG", now=now)

        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["decision"], "NO_TRADE")

    def test_missing_history_is_unknown_and_fail_closed(self):
        frame = make_daily_frame(self.closes_up[:150])
        now = frame.index[-1] + pd.Timedelta(hours=24)

        result = self.engine.evaluate(frame, base_signal="LONG", now=now)

        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["required_bars"], 220)

    def test_duplicate_timestamps_are_unknown(self):
        frame = make_daily_frame(self.closes_up)
        frame.index = list(frame.index[:-1]) + [frame.index[-2]]
        now = pd.Timestamp("2026-08-20T00:00:00Z")

        result = self.engine.evaluate(frame, base_signal="LONG", now=now)

        self.assertEqual(result["status"], "UNKNOWN")

    def test_zero_prior_volume_is_unknown_instead_of_assuming_confirmation(self):
        volumes = np.zeros(260)
        volumes[-1] = 1000.0
        frame = make_daily_frame(self.closes_up, volume=volumes)
        now = frame.index[-1] + pd.Timedelta(hours=24)

        result = self.engine.evaluate(frame, base_signal="LONG", now=now)

        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["decision"], "NO_TRADE")

    def test_config_rejects_invalid_reward_risk(self):
        with self.assertRaises(ValueError):
            MasterStrategyConfig(stop_atr_multiple=3.0, target_atr_multiple=2.0)


if __name__ == "__main__":
    unittest.main()
