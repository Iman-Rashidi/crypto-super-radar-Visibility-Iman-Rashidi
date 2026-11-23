from __future__ import annotations

import unittest
from unittest.mock import patch

import pandas as pd

from crypto_super_radar.live_market_radar import (
    LIVE_MARKET_RADAR_CACHE_TTL_SECONDS,
    fetch_coinbase_candles,
    get_live_market_radar_cache,
    set_live_market_radar_cache,
)
from crypto_super_radar import web_app


class FakeCoinbaseHttp:
    def get_with_timeout(self, url, params, timeout):
        self.url = url
        self.params = params
        self.timeout = timeout
        return [
            [1735689600, "99", "102", "100", "101", "12.5"],
            [1735689900, "100", "103", "101", "102", "15.0"],
        ]


class LiveMarketRadarCacheTests(unittest.TestCase):
    def test_cache_ttl_is_45_seconds_and_returns_a_copy(self):
        self.assertEqual(LIVE_MARKET_RADAR_CACHE_TTL_SECONDS, 45)
        key = ("CACHE_TEST_USDT", "5m")
        payload = {"candles": [{"close": 100.0}]}
        set_live_market_radar_cache(key, payload, now=100.0)

        fresh = get_live_market_radar_cache(key, now=144.999)
        self.assertIsNotNone(fresh)
        self.assertAlmostEqual(fresh[1], 44.999, places=3)
        fresh[0]["candles"][0]["close"] = 1.0
        self.assertEqual(get_live_market_radar_cache(key, now=144.0)[0]["candles"][0]["close"], 100.0)

        self.assertIsNone(get_live_market_radar_cache(key, now=145.0))
        stale = get_live_market_radar_cache(key, now=150.0, allow_expired=True)
        self.assertIsNotNone(stale)
        self.assertEqual(stale[0]["candles"][0]["close"], 100.0)

    def test_coinbase_exchange_rows_are_normalized_to_ohlcv(self):
        http = FakeCoinbaseHttp()
        frame = fetch_coinbase_candles(http, "BTCUSDT", "5m")
        self.assertEqual(http.params["granularity"], 300)
        self.assertEqual(http.timeout, 1.35)
        self.assertEqual(list(frame.columns), ["open", "high", "low", "close", "volume"])
        self.assertEqual(float(frame.iloc[-1]["open"]), 101.0)
        self.assertEqual(float(frame.iloc[-1]["high"]), 103.0)
        self.assertEqual(float(frame.iloc[-1]["low"]), 100.0)
        self.assertEqual(float(frame.iloc[-1]["close"]), 102.0)
        self.assertEqual(float(frame.iloc[-1]["volume"]), 15.0)

    def test_all_upstream_timeouts_return_last_valid_payload_as_stale(self):
        key = ("BTCUSDT", "5m")
        valid_payload = {
            "status": "PASS",
            "freshness_status": "FRESH",
            "candles": [{"close": 100.0}],
            "signal": {"signal": "WAIT", "status": "WAITING"},
            "market_analysis": {"data_quality": {"freshness_status": "FRESH", "signal_eligible": True}},
        }
        set_live_market_radar_cache(key, valid_payload, now=0.0)

        with (
            patch.object(web_app.ENGINE.data.http, "get_with_timeout", side_effect=TimeoutError("simulated Binance timeout")),
            patch.object(web_app, "fetch_bybit_candles", side_effect=TimeoutError("simulated Bybit timeout")),
            patch.object(web_app, "fetch_okx_candles", side_effect=TimeoutError("simulated OKX timeout")),
            patch.object(web_app, "fetch_kraken_candles", side_effect=TimeoutError("simulated Kraken timeout")),
            patch.object(web_app, "fetch_coinbase_candles", side_effect=TimeoutError("simulated Coinbase timeout")),
        ):
            result = web_app._live_market_radar_payload("BTCUSDT", "5m", force_refresh=True)

        self.assertEqual(result["cache_status"], "STALE_CACHE")
        self.assertEqual(result["status"], "STALE")
        self.assertEqual(result["signal"]["status"], "BLOCKED")
        self.assertEqual(result["signal"]["signal"], "UNKNOWN")
        self.assertFalse(result["market_analysis"]["data_quality"]["signal_eligible"])
        self.assertTrue(result["feed_diagnostics"]["stale_cache_fallback"])


if __name__ == "__main__":
    unittest.main()
