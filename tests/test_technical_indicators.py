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

    def test_macd_sar_atr_and_fibonacci_sections_are_returned(self):
        result = analyze_technical_indicators(self.candles())
        self.assertEqual(result["status"], "KNOWN")
        self.assertEqual(result["macd"]["fast_period"], 12)
        self.assertEqual(result["macd"]["slow_period"], 26)
        self.assertEqual(result["macd"]["signal_period"], 9)
        self.assertIsNotNone(result["macd"]["histogram"])
        self.assertIn(result["macd"]["crossover"], {"BULLISH_CROSS", "BEARISH_CROSS", "NONE", "UNKNOWN"})
        self.assertIsNotNone(result["parabolic_sar"]["value"])
        self.assertIn(result["parabolic_sar"]["trend"], {"BULLISH", "BEARISH"})
        self.assertGreater(result["atr"]["value"], 0)
        self.assertEqual(result["atr"]["period"], 14)
        self.assertEqual(result["atr"]["directional_signal"], "NONE_VOLATILITY_ONLY")
        self.assertIn("risk_references", result["atr"])
        fib = result["fibonacci"]
        if fib["status"] == "CALCULATED":
            self.assertEqual(set(fib["levels"]), {"23.6%", "38.2%", "50.0%", "61.8%", "78.6%"})
            self.assertIn(fib["direction"], {"BULLISH", "BEARISH"})

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


    def test_mfi_dpo_and_roc_sections_are_returned(self):
        result = analyze_technical_indicators(self.candles())
        self.assertEqual(result["status"], "KNOWN")
        mfi = result["mfi"]
        self.assertEqual(mfi["period"], 14)
        self.assertIn(mfi["state"], {"OVERBOUGHT", "OVERSOLD", "NEUTRAL", "UNKNOWN"})
        self.assertIn(mfi["divergence"], {"BULLISH", "BEARISH", "NONE"})
        self.assertEqual(mfi["overbought_threshold"], 80)
        self.assertEqual(mfi["oversold_threshold"], 20)
        dpo = result["dpo"]
        self.assertEqual(dpo["period"], 20)
        self.assertEqual(dpo["shift"], 11)
        self.assertIn(dpo["zero_line"], {"BULLISH_ZERO_CROSS", "BEARISH_ZERO_CROSS", "ABOVE_ZERO", "BELOW_ZERO", "UNKNOWN"})
        self.assertEqual(dpo["trend_identification"], "NOT_SUITABLE_CYCLE_ONLY")
        roc = result["roc"]
        self.assertEqual(roc["period"], 10)
        self.assertIsNotNone(roc["value_pct"])
        self.assertIn(roc["zero_line"], {"BULLISH_ZERO_CROSS", "BEARISH_ZERO_CROSS", "ABOVE_ZERO", "BELOW_ZERO", "UNKNOWN"})
        self.assertEqual(roc["overbought_oversold"], "NO_STANDARD_THRESHOLDS_USE_HISTORICAL_EXTREMES")

    def test_supertrend_williams_r_and_indicator_roles(self):
        result = analyze_technical_indicators(self.candles())
        self.assertEqual(result["status"], "KNOWN")
        st = result["supertrend"]
        self.assertEqual(st["atr_period"], 10)
        self.assertEqual(st["atr_multiplier"], 3.0)
        self.assertIsNotNone(st["value"])
        self.assertIn(st["trend"], {"BULLISH", "BEARISH"})
        self.assertIn(st["flip"], {"BULLISH_FLIP", "BEARISH_FLIP", "NONE"})
        self.assertEqual(st["trailing_stop_reference"], st["value"])
        wr = result["williams_r"]
        self.assertEqual(wr["period"], 14)
        self.assertIsNotNone(wr["value"])
        self.assertGreaterEqual(wr["value"], -100)
        self.assertLessEqual(wr["value"], 0)
        self.assertEqual(wr["overbought_threshold"], -20)
        self.assertEqual(wr["oversold_threshold"], -80)
        roles = result["indicator_roles"]
        self.assertIn("RSI", roles["leading_or_early_warning"])
        self.assertIn("Supertrend", roles["lagging_or_confirmation"])
        self.assertEqual(roles["order_authority"], "NONE_INDICATORS_ARE_EVIDENCE_ONLY")

if __name__ == "__main__":
    unittest.main()
