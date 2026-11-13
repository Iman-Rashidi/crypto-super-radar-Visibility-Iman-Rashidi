from __future__ import annotations

import unittest

import pandas as pd

from crypto_super_radar.market_flow import analyze_market_flow


class MarketFlowTests(unittest.TestCase):
    def setUp(self):
        index = pd.date_range("2026-01-01", periods=30, freq="1min", tz="UTC")
        self.frame = pd.DataFrame({
            "open": [99.0] * 30,
            "high": [101.0] * 30,
            "low": [98.0] * 30,
            "close": [100.0] * 30,
            "volume": [10.0] * 30,
        }, index=index)

    def test_buy_sell_pressure_is_colored_and_volume_is_quote_notional(self):
        self.frame.iloc[-1, self.frame.columns.get_loc("close")] = 100.8
        result = analyze_market_flow(self.frame)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertGreater(result["pressure"]["buy_pct"], 50)
        self.assertEqual(result["pressure"]["color"], "GREEN")
        self.assertAlmostEqual(result["current_quote_volume"], 1008.0)
        self.assertIsNotNone(result["rvol"])

    def test_aggressor_cvd_and_large_prints_are_separate_from_candle_estimate(self):
        trades = [
            {"p": "100", "q": "1", "m": False, "T": 1767225600000, "a": i}
            for i in range(24)
        ]
        trades.append({"p": "100", "q": "600", "m": True, "T": 1767225601000, "a": 99})
        book = {
            "bids": [[99.9, 20.0] for _ in range(20)],
            "asks": [[100.1, 5.0] for _ in range(20)],
        }
        result = analyze_market_flow(self.frame, aggregate_trades=trades, order_book=book)
        self.assertEqual(result["aggressor_flow"]["trade_count"], 25)
        self.assertLess(result["aggressor_flow"]["net_delta_quote"], 0)
        self.assertEqual(result["order_book"]["status"], "PASS")
        self.assertGreater(result["order_book"]["imbalance_pct"], 0)
        self.assertTrue(result["large_trades"])
        self.assertEqual(result["large_trades"][0]["classification"], "LARGE_PRINT_NOT_WALLET_ID")
        self.assertEqual(result["whale_flow"]["classification"], "INFERRED_FLOW_NOT_IDENTIFIED_WHALE")

    def test_missing_microstructure_data_fails_closed_without_inventing_whales(self):
        result = analyze_market_flow(self.frame, aggregate_trades=None, order_book=None)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["whale_flow"]["bias"], "MIXED")
        self.assertEqual(result["large_trades"], [])


if __name__ == "__main__":
    unittest.main()
