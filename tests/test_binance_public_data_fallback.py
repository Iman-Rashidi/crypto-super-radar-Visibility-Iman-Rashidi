"""Deterministic tests for Binance Spot public market-data host fallback."""
import unittest

from crypto_super_radar.providers import BinanceProvider, DataProviderError


class FakeHttp:
    def __init__(self, primary_error="HTTP 451"):
        self.primary_error = primary_error
        self.calls = []

    def get(self, url, params=None):
        self.calls.append(url)
        if "data-api.binance.vision" in url:
            raise DataProviderError(self.primary_error)
        if url.endswith("/api/v3/ticker/price"):
            return {"symbol": "BTCUSDT", "price": "65000"}
        if url.endswith("/api/v3/ticker/24hr"):
            return {"symbol": "BTCUSDT", "quoteVolume": "1000000"}
        raise AssertionError("Unexpected URL: " + url)


class BinancePublicDataFallbackTests(unittest.TestCase):
    def test_451_on_market_data_host_falls_back_to_official_spot_host(self):
        http = FakeHttp()
        result = BinanceProvider(http).ticker("BTCUSDT")
        self.assertEqual(result["price"], 65000.0)
        self.assertEqual(result["volume_24h"], 1000000.0)
        self.assertTrue(all("api.binance.com" in url for url in http.calls[1::2]))
        self.assertTrue(any("data-api.binance.vision" in url for url in http.calls))

    def test_non_access_errors_do_not_trigger_alternate_hosts(self):
        http = FakeHttp(primary_error="HTTP 400")
        with self.assertRaises(DataProviderError):
            BinanceProvider(http).ticker("BTCUSDT")
        self.assertEqual(len(http.calls), 1)
        self.assertIn("data-api.binance.vision", http.calls[0])


if __name__ == "__main__":
    unittest.main()
