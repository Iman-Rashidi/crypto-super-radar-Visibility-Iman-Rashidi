from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from crypto_super_radar.technical_indicators import analyze_technical_indicators


class EasyTraderTechnicalIndicatorTests(unittest.TestCase):
    def candles(self, count: int = 260) -> pd.DataFrame:
        x = np.arange(count, dtype=float)
        close = 100.0 + 0.12 * x + 2.0 * np.sin(x / 8.0)
        open_ = np.r_[close[0], close[:-1]]
        high = np.maximum(open_, close) + 0.8
        low = np.minimum(open_, close) - 0.8
        volume = 1000.0 + (x % 13) * 20.0
        index = pd.date_range("2025-01-01", periods=count, freq="D", tz="UTC")
        return pd.DataFrame(
            {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
            index=index,
        )

    def test_returns_ma_obv_and_rsi_sections(self):
        result = analyze_technical_indicators(self.candles())
        self.assertEqual(result["status"], "KNOWN")
        self.assertIn("sma20", result["moving_averages"])
        self.assertIn("ema200", result["moving_averages"])
        self.assertIn("envelopes", result["moving_averages"])
        self.assertIn("divergence", result["obv"])
        self.assertIn("breakout_confirmation", result["obv"])
        self.assertIn("midline_50", result["rsi"])
        self.assertIn("trendline_breakout", result["rsi"])

    def test_envelope_bands_are_symmetric_around_sma50(self):
        result = analyze_technical_indicators(self.candles(), envelope_pct=5)
        bands = result["moving_averages"]["envelopes"]
        middle = result["moving_averages"]["sma50"]
        self.assertAlmostEqual(bands["upper"], middle * 1.05)
        self.assertAlmostEqual(bands["lower"], middle * 0.95)

    def test_invalid_ohlcv_fails_closed(self):
        frame = self.candles()
        frame.loc[frame.index[-1], "volume"] = -1
        result = analyze_technical_indicators(frame)
        self.assertEqual(result["status"], "UNKNOWN")

    def test_invalid_envelope_percentage_fails_closed(self):
        result = analyze_technical_indicators(self.candles(), envelope_pct=50)
        self.assertEqual(result["status"], "UNKNOWN")

    def test_insufficient_history_is_unknown(self):
        result = analyze_technical_indicators(self.candles(20))
        self.assertEqual(result["status"], "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
