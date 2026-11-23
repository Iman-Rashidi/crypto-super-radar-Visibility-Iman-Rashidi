from __future__ import annotations

import unittest

from crypto_super_radar.future_forecast import build_future_forecast


def candles(base=100.0, count=40):
    return [
        {"open": base + i * .05, "high": base + i * .05 + .8,
         "low": base + i * .05 - .7, "close": base + i * .05 + .2,
         "volume": 1000, "time": i}
        for i in range(count)
    ]


class FutureForecastTests(unittest.TestCase):
    def test_confidence_band_and_invalidation_are_generated(self):
        result = build_future_forecast(
            candles(), symbol="BTCUSDT", interval="5m",
            wave={"pattern": "BULLISH", "wave_count": 3},
            divergence={"type": "BULLISH", "confirmed": True},
            market_flow={"whale_flow": {"bias": "BULLISH_FLOW", "label": "انباشت"},
                         "large_trades": [{"quote": 100000}],
                         "order_book": {"imbalance_pct": 18}},
            higher_timeframes={"1h": candles(100), "4h": candles(99)},
            news=[{"title": "BTC asset headline", "source": "CryptoCompare", "sentiment": "POSITIVE"}],
        )
        self.assertEqual(result["scenario"], "BULLISH")
        self.assertGreater(result["confidence_pct"], 50)
        self.assertEqual(len(result["confidence_band"]), 12)
        self.assertIsNotNone(result["invalidation_price"])
        self.assertIsNotNone(result["bullish_start"])
        self.assertIsNotNone(result["stagnation_start"])

    def test_missing_whale_and_news_are_not_fabricated(self):
        result = build_future_forecast(candles(), symbol="ETHUSDT", interval="5m")
        self.assertEqual(result["whale_summary_fa"], "داده تأییدشده موجود نیست")
        self.assertEqual(result["news_summary_fa"], "داده تأییدشده موجود نیست")

    def test_missing_candles_fail_closed(self):
        result = build_future_forecast([], symbol="BTCUSDT", interval="5m")
        self.assertEqual(result["status"], "INSUFFICIENT_DATA")
        self.assertIsNone(result["confidence_pct"])
        self.assertEqual(result["confidence_band"], [])


if __name__ == "__main__":
    unittest.main()
