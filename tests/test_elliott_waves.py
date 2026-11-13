from __future__ import annotations

import unittest

import pandas as pd

from crypto_super_radar.elliott_waves import monitor_elliott_waves


def candles_from_close(values):
    index = pd.date_range("2025-01-01", periods=len(values), freq="D", tz="UTC")
    return pd.DataFrame({"close": values}, index=index)


class ElliottWaveMonitorTests(unittest.TestCase):
    def test_confirms_rule_valid_bullish_five_wave_structure(self):
        values = [100, 100, 120, 120, 110, 110, 140, 140, 125, 125, 150, 150, 140]
        values = values + [140] * (35 - len(values))
        result = monitor_elliott_waves(candles_from_close(values))
        self.assertEqual(result["status"], "CONFIRMED")
        self.assertEqual(result["pattern"], "BULLISH")
        self.assertEqual(result["phase"], "FIVE_WAVE_IMPULSE_CONFIRMED")
        self.assertEqual(result["wave_count"], 5)
        self.assertEqual(result["invalidation_price"], 120.0)
        self.assertGreater(
            pd.Timestamp(result["pivots"][-1]["confirmed_at"]),
            pd.Timestamp(result["pivots"][-1]["timestamp"]),
        )

    def test_reports_valid_prefix_without_promoting_it_to_a_trade_signal(self):
        values = [100, 100, 120, 120, 110, 110, 140, 140, 125, 125, 130]
        values = values + [130] * (35 - len(values))
        result = monitor_elliott_waves(candles_from_close(values))
        self.assertEqual(result["status"], "CANDIDATE")
        self.assertEqual(result["pattern"], "BULLISH")
        self.assertEqual(result["phase"], "WAVE_4_CONFIRMED")
        self.assertEqual(result["next_wave"], 5)
        self.assertIn("not probabilities", result["confidence_semantics"])

    def test_invalid_wave_two_sequence_is_ambiguous(self):
        values = [100, 100, 120, 120, 95, 95, 100] + [100] * 28
        result = monitor_elliott_waves(candles_from_close(values))
        self.assertEqual(result["status"], "AMBIGUOUS")
        self.assertEqual(result["phase"], "NO_RULE_VALID_IMPULSE")
        self.assertIsNone(result["invalidation_price"])


    def test_intraday_threshold_detects_swings_below_daily_three_percent_floor(self):
        # A liquid intraday series can form several useful swings without a 3% reversal.
        values = [
            100.00, 100.10, 100.22, 100.38, 100.55, 100.42, 100.25, 100.08,
            99.96, 100.08, 100.25, 100.44, 100.63, 100.49, 100.30, 100.13,
            100.02, 100.15, 100.34, 100.54, 100.74, 100.60, 100.42, 100.22,
            100.12, 100.25, 100.45, 100.66, 100.87, 100.73, 100.55, 100.35,
            100.25, 100.39, 100.59, 100.80, 101.02, 100.88, 100.69, 100.50,
        ]
        frame = candles_from_close(values)
        daily_floor = monitor_elliott_waves(frame)
        intraday = monitor_elliott_waves(
            frame, min_reversal_pct=0.0015, max_reversal_pct=0.15
        )
        self.assertEqual(daily_floor["status"], "UNKNOWN")
        self.assertGreaterEqual(intraday["confirmed_pivot_count"], 3)
        self.assertNotEqual(intraday["phase"], "INSUFFICIENT_DATA")

    def test_missing_or_short_candle_history_is_unknown(self):
        self.assertEqual(monitor_elliott_waves(None)["status"], "UNKNOWN")
        self.assertEqual(monitor_elliott_waves(candles_from_close([1.0] * 20))["status"], "UNKNOWN")

    def test_invalid_candle_values_are_unknown(self):
        frame = candles_from_close([1.0] * 35)
        frame.iloc[-1, frame.columns.get_loc("close")] = float("nan")
        self.assertEqual(monitor_elliott_waves(frame)["status"], "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
