"""Unit tests for public OHLCV fallback adapters and source-aware radar metadata."""
import unittest
from unittest.mock import Mock
import pandas as pd

from crypto_super_radar.live_market_radar import fetch_okx_candles, fetch_kraken_candles


class LiveMarketRadarFallbackTests(unittest.TestCase):
    def test_okx_candles_are_normalized_from_millisecond_timestamps(self):
        ts = 1795460400000
        http = Mock()
        http.get_with_timeout.return_value = {
            "code": "0",
            "data": [[str(ts), "100", "103", "99", "102", "12.5", "12.5", "1275", "1"]],
        }
        frame = fetch_okx_candles(http, "BTCUSDT", "5m", 240)
        self.assertEqual(len(frame), 1)
        self.assertEqual(float(frame.iloc[0]["close"]), 102.0)
        self.assertEqual(float(frame.iloc[0]["volume"]), 12.5)
        args = http.get_with_timeout.call_args
        self.assertEqual(args.args[0], "https://www.okx.com/api/v5/market/candles")
        self.assertEqual(args.kwargs["timeout"], 1.35)

    def test_okx_api_errors_are_not_silently_accepted(self):
        http = Mock()
        http.get_with_timeout.return_value = {"code": "51000", "msg": "bad instrument"}
        with self.assertRaisesRegex(ValueError, "OKX candle API error"):
            fetch_okx_candles(http, "BTCUSDT", "5m")

    def test_kraken_uses_volume_not_vwap_and_maps_btc_to_xbt(self):
        http = Mock()
        http.get_with_timeout.return_value = {
            "error": [],
            "result": {
                "XXBTUSDT": [[1795460400, "100", "103", "99", "102", "101.5", "7.25", 42]],
                "last": 1795460400,
            },
        }
        frame = fetch_kraken_candles(http, "BTCUSDT", "5m")
        self.assertEqual(float(frame.iloc[0]["close"]), 102.0)
        self.assertEqual(float(frame.iloc[0]["volume"]), 7.25)
        self.assertEqual(http.get_with_timeout.call_args.args[1]["pair"], "XBTUSDT")

    def test_kraken_rejects_unsupported_interval(self):
        with self.assertRaisesRegex(ValueError, "Kraken does not support interval"):
            fetch_kraken_candles(Mock(), "BTCUSDT", "1s")


if __name__ == "__main__":
    unittest.main()
