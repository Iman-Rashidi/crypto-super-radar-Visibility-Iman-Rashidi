from __future__ import annotations

import unittest
from unittest.mock import patch

import pandas as pd

from crypto_super_radar.challenge1_v2 import (
    breakout_volume_confirmation,
    calculate_wave4_fibonacci,
    rsi_divergence_entry_guard,
    build_wave5_entry_signal,
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

    def test_w4_pullback_confirms_w5_entry_with_structural_stop(self):
        idx5 = pd.date_range("2026-01-01", periods=60, freq="5min", tz="UTC")
        close5 = [100 + i * 0.25 for i in range(60)]
        frame5 = pd.DataFrame({
            "open":[x-0.1 for x in close5], "high":[x+0.2 for x in close5],
            "low":[x-0.2 for x in close5], "close":close5,
            "volume":[100]*59+[130],
        }, index=idx5)
        frame5.iloc[-2, frame5.columns.get_loc("high")] = 115.5
        frame5.iloc[-1, frame5.columns.get_loc("close")] = 116.0
        frame5.iloc[-1, frame5.columns.get_loc("high")] = 116.2
        idx15 = pd.date_range("2026-01-01", periods=30, freq="15min", tz="UTC")
        close15 = [100 + i * 0.5 for i in range(30)]
        frame15 = pd.DataFrame({
            "open":[x-0.1 for x in close15], "high":[x+0.2 for x in close15],
            "low":[x-0.2 for x in close15], "close":close15,
            "volume":[100]*30,
        }, index=idx15)
        asof = idx5[-1] + pd.Timedelta(minutes=5)
        pivots = [
            {"kind":"LOW","price":100,"timestamp":idx5[5].isoformat(),"confirmed_at":idx5[7].isoformat(),"index":5},
            {"kind":"HIGH","price":110,"timestamp":idx5[12].isoformat(),"confirmed_at":idx5[14].isoformat(),"index":12},
            {"kind":"LOW","price":105,"timestamp":idx5[20].isoformat(),"confirmed_at":idx5[22].isoformat(),"index":20},
            {"kind":"HIGH","price":125,"timestamp":idx5[35].isoformat(),"confirmed_at":idx5[37].isoformat(),"index":35},
            {"kind":"LOW","price":115,"timestamp":idx5[45].isoformat(),"confirmed_at":idx5[47].isoformat(),"index":45},
        ]
        wave = {"phase":"WAVE_4_CONFIRMED","pattern":"BULLISH","pivots":pivots}
        funding_time = int(asof.timestamp() * 1000) - 60_000
        with patch("crypto_super_radar.challenge1_v2.monitor_elliott_waves", return_value=wave):
            result = build_wave5_entry_signal(
                instrument_id="BTC-USDT", frame_5m=frame5, frame_15m=frame15,
                asof=asof, entry_price=116.0, funding_rate=0.0001,
                funding_kind="0", funding_event_time_ms=funding_time,
                funding_max_age_ms=3_600_000, spread_fraction=0.0001,
            )
        self.assertEqual(result["signal"], "LONG")
        self.assertTrue(result["wave5_entry_active"])
        self.assertLess(result["stop_loss"], 112.64)
        self.assertGreater(result["take_profit"], result["entry"])
        self.assertGreaterEqual(result["gross_rr"], 1.8)


if __name__ == "__main__":
    unittest.main()
