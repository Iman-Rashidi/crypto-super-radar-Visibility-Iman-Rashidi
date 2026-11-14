from __future__ import annotations

import unittest
import numpy as np
import pandas as pd

from crypto_super_radar.price_action import analyze_easy_price_action


class EasyPriceActionTests(unittest.TestCase):
    def candles(self, count=100):
        x = np.arange(count, dtype=float)
        close = 100 + x * 0.15 + np.sin(x / 3) * 2
        open_ = np.r_[close[0], close[:-1]]
        high = np.maximum(open_, close) + 0.8
        low = np.minimum(open_, close) - 0.8
        volume = 1000 + (x % 9) * 100
        index = pd.date_range("2026-01-01", periods=count, freq="h", tz="UTC")
        return pd.DataFrame({"open":open_,"high":high,"low":low,"close":close,
                             "volume":volume}, index=index)

    def test_returns_all_four_diagnostic_families_without_hard_gate(self):
        result = analyze_easy_price_action(self.candles())
        self.assertEqual(result["status"], "KNOWN")
        self.assertIn("support_zones", result["support_resistance"])
        self.assertIn("ascending_support", result["trendlines"])
        self.assertIn("relative_volume_20", result["volume"])
        self.assertIn("events", result["gaps"])
        self.assertFalse(result["data_policy"]["used_as_hard_gate"])

    def test_missing_volume_is_unknown_not_fabricated(self):
        frame = self.candles().drop(columns=["volume"])
        result = analyze_easy_price_action(frame)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["volume"]["status"], "UNKNOWN")

    def test_duplicate_timestamps_are_rejected(self):
        frame = self.candles()
        frame.index = list(frame.index[:-1]) + [frame.index[-2]]
        result = analyze_easy_price_action(frame)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertIn("Unique", result["reason"])

    def test_out_of_order_timestamps_are_rejected(self):
        frame = self.candles()
        frame = frame.iloc[::-1]
        result = analyze_easy_price_action(frame)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertIn("chronological", result["reason"])

    def test_malformed_ohlc_is_rejected(self):
        frame = self.candles()
        frame.iloc[-2, frame.columns.get_loc("high")] = 1
        result = analyze_easy_price_action(frame)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertIn("Malformed", result["reason"])

    def test_live_last_candle_is_excluded_from_confirmed_as_of(self):
        frame = self.candles()
        result = analyze_easy_price_action(frame)
        self.assertNotEqual(result["as_of"], frame.index[-1].isoformat())
        self.assertTrue(result["data_policy"]["last_candle_excluded_as_provisional"])

    def test_explicitly_closed_last_candle_is_included(self):
        frame = self.candles()
        result = analyze_easy_price_action(frame, last_candle_closed=True)
        self.assertEqual(result["as_of"], frame.index[-1].isoformat())
        self.assertFalse(result["data_policy"]["last_candle_excluded_as_provisional"])

    def test_volume_climax_and_range_obv_diagnostics_are_present(self):
        frame = self.candles()
        frame.iloc[-2, frame.columns.get_loc("volume")] = 20000
        result = analyze_easy_price_action(frame)
        self.assertTrue(result["volume"]["volume_climax"])
        self.assertIn(result["volume"]["range_obv_behavior"], {
            "POSSIBLE_ACCUMULATION", "POSSIBLE_DISTRIBUTION", "NO_CLEAR_RANGE_PHASE"
        })

    def test_gap_event_has_candidate_type_and_fill_status(self):
        frame = self.candles()
        i = 60
        previous_close = float(frame["close"].iloc[i-1])
        frame.iloc[i, frame.columns.get_loc("open")] = previous_close * 1.02
        frame.iloc[i, frame.columns.get_loc("high")] = max(
            frame.iloc[i]["open"], frame.iloc[i]["close"]
        ) + 0.8
        frame.iloc[i, frame.columns.get_loc("low")] = min(
            frame.iloc[i]["open"], frame.iloc[i]["close"]
        ) - 0.8
        result = analyze_easy_price_action(frame, gap_threshold_pct=1.0)
        self.assertGreaterEqual(result["gaps"]["count"], 1)
        event = next(x for x in result["gaps"]["events"] if x["timestamp"] == frame.index[i].isoformat())
        self.assertIn(event["status"], {"FILLED", "UNFILLED"})
        self.assertIn(event["gap_type"], {
            "COMMON_OR_FILLED", "UNCLASSIFIED", "BREAKAWAY_CANDIDATE",
            "EXHAUSTION_CANDIDATE", "RUNAWAY_CANDIDATE"
        })

    def test_timestamp_gap_is_not_misreported_as_price_gap(self):
        frame = self.candles()
        frame.index = frame.index.where(
            np.arange(len(frame)) < 60,
            frame.index + pd.to_timedelta((np.arange(len(frame)) >= 60).astype(int), unit="h")
        )
        # A timestamp discontinuity is deliberately excluded from price-gap events.
        result = analyze_easy_price_action(frame)
        self.assertEqual(result["status"], "KNOWN")
        self.assertIn("data gaps", result["gaps"]["note"])


if __name__ == "__main__":
    unittest.main()
