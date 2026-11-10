from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from crypto_super_radar.market_behavior import analyze_market_behavior


class MarketBehaviorTests(unittest.TestCase):
    def candles(self, count=520):
        rng = np.random.default_rng(731)
        returns = rng.normal(0.0005, 0.025, count)
        close = 100.0 * np.exp(np.cumsum(returns))
        prior = np.r_[close[0], close[:-1]]
        open_ = prior * (1 + rng.normal(0, 0.003, count))
        high = np.maximum(open_, close) * (1 + rng.uniform(0.001, 0.025, count))
        low = np.minimum(open_, close) * (1 - rng.uniform(0.001, 0.025, count))
        end = pd.Timestamp.now(tz="UTC").floor("D")
        index = pd.date_range(end=end, periods=count, freq="D")
        return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close}, index=index)

    def test_analogue_error_is_reported_with_wilson_uncertainty(self):
        frame = self.candles()
        result = analyze_market_behavior(
            frame, frame.close.iloc[-1], side="LONG", horizon_days=3, minimum_analogs=5,
        )
        stats = result["historical_analogues"]
        self.assertGreaterEqual(stats["analogue_count"], 5)
        self.assertGreaterEqual(stats["error_rate_upper95_pct"], stats["estimated_error_pct"])
        self.assertEqual(
            round(100 - stats["win_rate_pct"], 2), stats["estimated_error_pct"],
        )
        self.assertIn("Wilson", stats["method"])

    def test_price_structure_exposes_fibonacci_and_protective_level_fields(self):
        frame = self.candles()
        result = analyze_market_behavior(frame, frame.close.iloc[-1], minimum_analogs=5)
        self.assertIn("levels", result)
        self.assertIn("stop_loss", result["levels"])
        self.assertIn("take_profit", result["levels"])
        self.assertIn("0.618", result["fibonacci"]["levels"])

    def test_malformed_ohlc_fails_closed(self):
        frame = self.candles()
        frame.iloc[-1, frame.columns.get_loc("high")] = 1.0
        result = analyze_market_behavior(frame, frame.close.iloc[-1])
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertIsNone(result["estimated_error_pct"])

    def test_future_dated_candles_fail_closed(self):
        frame = self.candles()
        frame.index = frame.index + pd.Timedelta(days=30)
        result = analyze_market_behavior(frame, frame.close.iloc[-1])
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertIn("Future-dated", result["reason"])

    def test_non_daily_history_is_not_misrepresented_as_daily_patterns(self):
        frame = self.candles()
        frame.index = pd.date_range(end=frame.index[-1], periods=len(frame), freq="h")
        result = analyze_market_behavior(frame, frame.close.iloc[-1])
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertIn("daily candles", result["reason"])

    def test_invalid_quote_and_incomplete_direction_fail_closed(self):
        frame = self.candles()
        self.assertEqual(
            analyze_market_behavior(frame, 0)["status"], "UNKNOWN",
        )
        self.assertEqual(
            analyze_market_behavior(frame, frame.close.iloc[-1], side="NO TRADE")["status"],
            "UNKNOWN",
        )


if __name__ == "__main__":
    unittest.main()
