from __future__ import annotations

import unittest

import pandas as pd

from crypto_super_radar.candle_quality import completed_daily_candles


class CompletedDailyCandleTests(unittest.TestCase):
    def setUp(self):
        index = pd.date_range("2026-11-01", periods=5, freq="D", tz="UTC")
        self.frame = pd.DataFrame({"close": [100, 101, 102, 103, 104]}, index=index)

    def test_drops_still_open_latest_daily_candle(self):
        now = pd.Timestamp("2026-11-05T12:00:00Z")
        result = completed_daily_candles(self.frame, now=now)
        self.assertEqual(len(result), 4)
        self.assertEqual(result.index[-1], self.frame.index[-2])

    def test_keeps_latest_candle_after_daily_interval_closes(self):
        now = pd.Timestamp("2026-11-06T00:00:00Z")
        result = completed_daily_candles(self.frame, now=now)
        self.assertEqual(len(result), 5)

    def test_does_not_rewrite_duplicate_timestamps(self):
        duplicate = pd.concat([self.frame, self.frame.iloc[[-1]]])
        result = completed_daily_candles(
            duplicate, now=pd.Timestamp("2026-11-05T12:00:00Z")
        )
        self.assertEqual(len(result), len(duplicate))


if __name__ == "__main__":
    unittest.main()
