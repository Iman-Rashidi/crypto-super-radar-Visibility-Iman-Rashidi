from __future__ import annotations

import unittest
from types import SimpleNamespace

from crypto_super_radar.providers import (
    BinanceProvider,
    DataProviderError,
    MarketDataService,
)


class FakeHttp:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, url, params=None):
        self.calls.append((url, params))
        return self.payload


class BinanceSpotOrderBookTests(unittest.TestCase):
    def test_returns_best_prices_base_quantities_and_receive_timestamp(self):
        http = FakeHttp({
            "symbol": "BTCUSDT",
            "bidPrice": "100.10", "bidQty": "2.5",
            "askPrice": "100.20", "askQty": "1.75",
        })
        result = BinanceProvider(http).order_book_top("btcusdt")
        self.assertEqual(http.calls, [(
            "https://data-api.binance.vision/api/v3/ticker/bookTicker",
            {"symbol": "BTCUSDT"},
        )])
        self.assertEqual(result["best_bid"], 100.10)
        self.assertEqual(result["best_ask"], 100.20)
        self.assertEqual(result["available_liquidity_buy_qty"], 1.75)
        self.assertEqual(result["available_liquidity_sell_qty"], 2.5)
        self.assertGreater(result["observed_at_ms"], 0)
        self.assertEqual(result["timestamp_kind"], "LOCAL_RECEIVED_AT")
        self.assertFalse(result["orders_submitted"])

    def test_refuses_invalid_symbol_before_http_request(self):
        http = FakeHttp({})
        with self.assertRaisesRegex(DataProviderError, "Invalid"):
            BinanceProvider(http).order_book_top("BTC/USDT")
        self.assertEqual(http.calls, [])

    def test_rejects_invalid_schema_and_symbol_mismatch(self):
        for payload in (None, [], {"symbol": "ETHUSDT", "bidPrice": "100", "askPrice": "101", "bidQty": "1", "askQty": "1"}):
            with self.subTest(payload=payload):
                with self.assertRaises(DataProviderError):
                    BinanceProvider(FakeHttp(payload)).order_book_top("BTCUSDT")

    def test_rejects_missing_zero_or_non_finite_depth_quantity(self):
        for value in ("0", "NaN", None):
            with self.subTest(value=value):
                payload = {
                    "symbol": "BTCUSDT", "bidPrice": "100", "askPrice": "101",
                    "bidQty": "1", "askQty": value,
                }
                with self.assertRaisesRegex(DataProviderError, "positive price or base quantity"):
                    BinanceProvider(FakeHttp(payload)).order_book_top("BTCUSDT")

    def test_rejects_crossed_order_book(self):
        payload = {
            "symbol": "BTCUSDT", "bidPrice": "102", "askPrice": "101",
            "bidQty": "1", "askQty": "1",
        }
        with self.assertRaisesRegex(DataProviderError, "crossed book"):
            BinanceProvider(FakeHttp(payload)).order_book_top("BTCUSDT")

    def test_market_data_service_limits_snapshot_to_cex_spot(self):
        service = MarketDataService.__new__(MarketDataService)
        service.binance = SimpleNamespace(
            order_book_top=lambda symbol: {"symbol": symbol, "source": "TEST"}
        )
        result = service.order_book_snapshot(
            SimpleNamespace(market_type="cex", symbol="BTCUSDT")
        )
        self.assertEqual(result["symbol"], "BTCUSDT")
        for asset in (
            SimpleNamespace(market_type="perp", symbol="BTCUSDT"),
            SimpleNamespace(market_type="dex", symbol=None),
        ):
            with self.subTest(asset=asset):
                with self.assertRaisesRegex(DataProviderError, "CEX Spot"):
                    service.order_book_snapshot(asset)


if __name__ == "__main__":
    unittest.main()
