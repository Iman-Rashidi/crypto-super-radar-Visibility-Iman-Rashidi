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


class MarketDataServiceBinanceBlockedTests(unittest.TestCase):
    def _frames(self):
        index = pd.date_range("2026-11-20", periods=8, freq="D", tz="UTC")
        base = pd.DataFrame({
            "open": [99.0] * 8,
            "high": [102.0] * 8,
            "low": [98.0] * 8,
            "close": [100.0, 100.2, 99.8, 100.1, 100.3, 100.0, 100.4, 100.5],
            "volume": [10.0] * 8,
        }, index=index)
        return {"OKX": base.copy(), "Bybit": base.copy(), "Kraken": base.copy()}

    def _service(self):
        from crypto_super_radar.models import AssetSpec
        from crypto_super_radar.providers import DataProviderError, MarketDataService

        service = MarketDataService()
        service.binance.ticker = Mock(side_effect=DataProviderError("HTTP 418 from api.binance.com"))
        service.binance.candles = Mock(side_effect=DataProviderError("HTTP 418 from api.binance.com"))
        service._fetch_cex_fallback_frames = Mock(return_value=self._frames())
        return service, AssetSpec.parse("cex:BTCUSDT")

    def test_snapshot_uses_independent_okx_bybit_kraken_after_binance_418(self):
        service, asset = self._service()
        snapshot = service.snapshot(asset)
        self.assertGreater(snapshot.price_usd, 0)
        self.assertIn(snapshot.provider_a, {"OKX", "Bybit", "Kraken"})
        self.assertIn(snapshot.provider_b, {"OKX", "Bybit", "Kraken"})
        self.assertNotEqual(snapshot.provider_a, snapshot.provider_b)
        self.assertTrue(snapshot.raw["fallback"])
        self.assertFalse(snapshot.raw["orders_submitted"])
        self.assertEqual(service._fetch_cex_fallback_frames.call_args.args[0], "BTCUSDT")

    def test_candles_return_validated_fallback_history_after_binance_418(self):
        service, asset = self._service()
        from crypto_super_radar.models import MarketSnapshot

        snapshot = MarketSnapshot(
            asset=asset, price_usd=100.0, volume_24h_usd=1000.0,
            liquidity_usd=None, price_discrepancy_pct=0.0,
            volume_discrepancy_pct=None, provider_a="OKX", provider_b="Bybit",
        )
        frame = service.candles(asset, snapshot)
        self.assertFalse(frame.empty)
        self.assertEqual(float(frame.iloc[-1]["close"]), 100.5)
        self.assertEqual(list(frame.columns), ["open", "high", "low", "close", "volume"])
        self.assertEqual(service._fetch_cex_fallback_frames.call_args.args[0], "BTCUSDT")



if __name__ == "__main__":
    unittest.main()
