"""Tests for validated market range summaries and spot scenario arithmetic."""
from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from crypto_super_radar.indicators import (
    observed_price_ranges,
    simulate_spot_long,
    technical_metrics,
)


def daily_ohlc(days: int = 400) -> pd.DataFrame:
    index = pd.date_range(
        end=pd.Timestamp.now(tz="UTC").floor("D"), periods=days, freq="D"
    )
    close = pd.Series(np.linspace(10.0, 20.0, days), index=index)
    return pd.DataFrame({
        "open": close * 0.999,
        "high": close * 1.02,
        "low": close * 0.98,
        "close": close,
    })


class ObservedPriceRangeTests(unittest.TestCase):
    def test_returns_short_and_long_observed_high_low(self):
        result = observed_price_ranges(daily_ohlc())
        self.assertEqual(result["status"], "KNOWN")
        for window in ("7", "30", "90", "365"):
            self.assertEqual(result["windows"][window]["status"], "KNOWN")
            self.assertGreater(result["windows"][window]["high"], result["windows"][window]["low"])
            self.assertGreaterEqual(result["windows"][window]["coverage_pct"], 80.0)

    def test_insufficient_history_is_unknown_not_fabricated(self):
        result = observed_price_ranges(daily_ohlc(20))
        self.assertEqual(result["status"], "KNOWN")
        self.assertEqual(result["windows"]["7"]["status"], "KNOWN")
        self.assertEqual(result["windows"]["30"]["status"], "UNKNOWN")
        self.assertIsNone(result["windows"]["30"]["high"])
        self.assertEqual(result["windows"]["365"]["status"], "UNKNOWN")

    def test_stale_candles_are_unknown(self):
        old = daily_ohlc()
        old.index = old.index - pd.Timedelta(days=5)
        result = observed_price_ranges(old)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertIn("stale", result["reason"])

    def test_malformed_ohlc_is_unknown(self):
        candles = daily_ohlc()
        candles.iloc[-1, candles.columns.get_loc("high")] = 0.1
        result = observed_price_ranges(candles)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertIn("Malformed", result["reason"])

    def test_duplicate_timestamps_are_unknown(self):
        candles = daily_ohlc()
        candles = pd.concat([candles, candles.iloc[[-1]]]).sort_index()
        result = observed_price_ranges(candles)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertIn("Duplicate", result["reason"])


class SpotScenarioCalculatorTests(unittest.TestCase):
    def test_ten_euro_flat_market_loses_fees_and_slippage(self):
        result = simulate_spot_long(10.0, 0.0)
        self.assertEqual(result["investment_eur"], 10.0)
        self.assertLess(result["net_pnl_eur"], 0.0)
        self.assertGreater(result["break_even_price_change_pct"], 0.0)
        self.assertAlmostEqual(
            result["net_return_pct"], result["net_pnl_eur"] / 10.0 * 100.0
        )

    def test_positive_and_negative_scenarios_have_correct_sign(self):
        gain = simulate_spot_long(10.0, 20.0)
        loss = simulate_spot_long(10.0, -20.0)
        self.assertGreater(gain["net_pnl_eur"], 0.0)
        self.assertLess(loss["net_pnl_eur"], 0.0)

    def test_zero_cost_scenario_matches_simple_return(self):
        result = simulate_spot_long(
            10.0, 15.0, buy_fee_pct=0, sell_fee_pct=0,
            buy_slippage_pct=0, sell_slippage_pct=0,
        )
        self.assertAlmostEqual(result["net_pnl_eur"], 1.5)
        self.assertAlmostEqual(result["break_even_price_change_pct"], 0.0)

    def test_invalid_amount_and_total_loss_are_rejected(self):
        with self.assertRaises(ValueError):
            simulate_spot_long(0.0, 10.0)
        with self.assertRaises(ValueError):
            simulate_spot_long(10.0, -100.0)


class AssetRegimeTests(unittest.TestCase):
    def test_technical_metrics_exposes_three_state_asset_regime(self):
        candles = daily_ohlc()
        benchmark = daily_ohlc()
        result = technical_metrics(candles, benchmark)
        self.assertEqual(result["status"], "KNOWN")
        self.assertIn(result["asset_regime"], {"BULLISH", "BEARISH", "NEUTRAL"})


if __name__ == "__main__":
    unittest.main()
