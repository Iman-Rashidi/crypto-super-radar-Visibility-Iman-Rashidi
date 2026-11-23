from __future__ import annotations

import unittest
from unittest.mock import patch

import pandas as pd

from crypto_super_radar.challenge1_v2 import (
    breakout_volume_confirmation,
    calculate_wave4_fibonacci,
    rsi_divergence_entry_guard,
)


class TestChallenge1WaveProtection(unittest.TestCase):
    def test_bearish_divergence_blocks_long(self):
        frame = pd.DataFrame({"open":[1]*30,"high":[2]*30,"low":[0.5]*30,"close":[1]*30,"volume":[10]*30},
                             index=pd.date_range("2026-01-01", periods=30, freq="5min", tz="UTC"))
        with patch("crypto_super_radar.challenge1_v2.detect_confirmed_rsi_divergence",
                   side_effect=[{"type":"BEARISH","timeframe":"5m"},{"type":"NONE","timeframe":"15m"}]):
            result = rsi_divergence_entry_guard(frame, frame, "LONG")
        self.assertTrue(result["blocked"])
        self.assertEqual(result["reason"], "RSI_DIVERGENCE_OPPOSES_ENTRY")

    def test_bullish_divergence_blocks_short(self):
        frame = pd.DataFrame({"open":[1]*30,"high":[2]*30,"low":[0.5]*30,"close":[1]*30,"volume":[10]*30},
                             index=pd.date_range("2026-01-01", periods=30, freq="5min", tz="UTC"))
        with patch("crypto_super_radar.challenge1_v2.detect_confirmed_rsi_divergence",
                   side_effect=[{"type":"NONE","timeframe":"5m"},{"type":"BULLISH","timeframe":"15m"}]):
            result = rsi_divergence_entry_guard(frame, frame, "SHORT")
        self.assertTrue(result["blocked"])

    def test_wave4_fibonacci_levels_from_confirmed_five_pivots(self):
        frame = pd.DataFrame({"close":[100]*30}, index=pd.date_range("2026-01-01", periods=30, freq="5min", tz="UTC"))
        wave = {
            "phase":"WAVE_4_CONFIRMED","pattern":"BULLISH",
            "pivots":[
                {"kind":"LOW","price":100},
                {"kind":"HIGH","price":110},
                {"kind":"LOW","price":105},
                {"kind":"HIGH","price":125},
                {"kind":"LOW","price":115},
            ],
        }
        with patch("crypto_super_radar.challenge1_v2.monitor_elliott_waves", return_value=wave):
            result = calculate_wave4_fibonacci(frame)
        self.assertEqual(result["status"], "CONFIRMED")
        self.assertAlmostEqual(result["levels"]["0.382"], 117.36)
        self.assertAlmostEqual(result["levels"]["0.5"], 115.0)
        self.assertAlmostEqual(result["levels"]["0.618"], 112.64)

    def test_breakout_volume_requires_twenty_candle_baseline(self):
        frame = pd.DataFrame({"volume":[100]*20+[130]})
        result = breakout_volume_confirmation(frame)
        self.assertTrue(result["confirmed"])
        self.assertAlmostEqual(result["volume_ratio"], 1.3)


if __name__ == "__main__":
    unittest.main()
