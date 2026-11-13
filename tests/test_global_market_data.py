from __future__ import annotations

import math
import time
import unittest
from datetime import datetime, timezone

from crypto_super_radar.global_market_data import (
    GlobalInstrument,
    GlobalMarketBoard,
    TwelveDataProvider,
)
from crypto_super_radar.providers import DataProviderError


class FakeHttp:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def request_json(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.payload


class TwelveDataProviderTests(unittest.TestCase):
    def setUp(self):
        self.instrument = GlobalInstrument(
            "us:aapl", "US_STOCK", "US", "AAPL", "Apple",
            "XNAS", "USD", "BASIC", "REALTIME",
        )

    def test_quote_uses_header_auth_and_marks_single_source_as_not_trade_eligible(self):
        http = FakeHttp({
            "symbol": "AAPL",
            "price": "231.50",
            "currency": "USD",
            "timestamp": int(time.time()),
            "volume": "123456",
            "is_market_open": True,
        })
        provider = TwelveDataProvider(http, api_key="test-secret")
        quote = provider.quote(self.instrument)

        self.assertEqual(quote["status"], "OBSERVED_SINGLE_SOURCE")
        self.assertEqual(quote["validation_status"], "SINGLE_SOURCE_ONLY")
        self.assertFalse(quote["trade_eligible"])
        self.assertFalse(quote["orders_submitted"])
        self.assertEqual(quote["price"], 231.5)
        self.assertEqual(quote["currency"], "USD")
        self.assertEqual(http.calls[0][2]["headers"]["Authorization"], "apikey test-secret")
        self.assertNotIn("apikey", http.calls[0][2].get("params", {}))
        self.assertNotIn("test-secret", str(quote))

    def test_missing_key_fails_closed_without_request(self):
        http = FakeHttp({"symbol": "AAPL", "price": "100", "timestamp": int(time.time())})
        provider = TwelveDataProvider(http, api_key="")
        with self.assertRaisesRegex(DataProviderError, "not configured"):
            provider.quote(self.instrument)
        self.assertEqual(http.calls, [])

    def test_quote_rejects_symbol_mismatch_and_missing_timestamp(self):
        http = FakeHttp({
            "symbol": "MSFT",
            "price": "100",
            "timestamp": int(time.time()),
        })
        provider = TwelveDataProvider(http, api_key="test-secret")
        with self.assertRaisesRegex(DataProviderError, "symbol"):
            provider.quote(self.instrument)

        http.payload = {"symbol": "AAPL", "price": "100"}
        with self.assertRaisesRegex(DataProviderError, "timestamp"):
            provider.quote(self.instrument)

    def test_provider_plan_errors_are_not_treated_as_market_data(self):
        http = FakeHttp({
            "status": "error",
            "code": 403,
            "message": "This symbol requires a higher plan",
        })
        provider = TwelveDataProvider(http, api_key="test-secret")
        with self.assertRaisesRegex(DataProviderError, "403"):
            provider.quote(self.instrument)

    def test_time_series_normalizes_exchange_time_and_preserves_missing_volume(self):
        http = FakeHttp({
            "meta": {"exchange_timezone": "America/New_York"},
            "values": [
                {"datetime": "2026-11-12", "open": "10", "high": "12", "low": "9", "close": "11", "volume": None},
                {"datetime": "2026-11-11", "open": "9", "high": "11", "low": "8", "close": "10", "volume": "100"},
            ],
        })
        provider = TwelveDataProvider(http, api_key="test-secret")
        frame = provider.time_series(self.instrument, outputsize=2)

        self.assertEqual(list(frame.columns), ["open", "high", "low", "close", "volume"])
        self.assertEqual(len(frame), 2)
        self.assertEqual(str(frame.index.tz), "UTC")
        self.assertTrue(math.isnan(frame.iloc[-1]["volume"]))
        self.assertEqual(frame.iloc[-1]["close"], 11.0)

    def test_time_series_rejects_bad_ohlc(self):
        http = FakeHttp({
            "meta": {"exchange_timezone": "UTC"},
            "values": [
                {"datetime": "2026-11-12", "open": "10", "high": "8", "low": "9", "close": "11", "volume": "1"}
            ],
        })
        provider = TwelveDataProvider(http, api_key="test-secret")
        with self.assertRaisesRegex(DataProviderError, "high/low"):
            provider.time_series(self.instrument, outputsize=1)


class GlobalMarketBoardTests(unittest.TestCase):
    def setUp(self):
        self.instrument = GlobalInstrument(
            "fx:eurusd", "FX", "GLOBAL", "EUR/USD", "EUR / USD",
            None, "USD", "BASIC", "REALTIME",
        )

    def test_missing_key_is_reported_without_network_call(self):
        board = GlobalMarketBoard(FakeHttp({}), instruments=(self.instrument,), provider=TwelveDataProvider(FakeHttp({}), api_key=""))
        result = board.get_board()
        self.assertEqual(result["status"], "CONFIGURATION_REQUIRED")
        self.assertFalse(result["configured"])
        self.assertFalse(result["trade_eligible"])
        self.assertEqual(result["items"][0]["status"], "API_KEY_MISSING")

    def test_successful_quote_never_marks_board_trade_eligible(self):
        class FakeProvider:
            configured = True

            def quote(self, instrument):
                return {
                    **instrument.to_public_dict(),
                    "status": "OBSERVED_SINGLE_SOURCE",
                    "freshness": "WITHIN_LAST_SESSION",
                    "price": 1.08,
                    "currency": "USD",
                    "provider": "Twelve Data",
                    "validation_status": "SINGLE_SOURCE_ONLY",
                    "trade_eligible": False,
                    "orders_submitted": False,
                }

        board = GlobalMarketBoard(
            FakeHttp({}), instruments=(self.instrument,), provider=FakeProvider(), cache_ttl_seconds=60
        )
        result = board.get_board()
        deadline = time.monotonic() + 2
        while result["status"] == "LOADING" and time.monotonic() < deadline:
            time.sleep(0.01)
            result = board.get_board()

        self.assertEqual(result["status"], "OBSERVED_SINGLE_SOURCE_NOT_VALIDATED")
        self.assertFalse(result["trade_eligible"])
        self.assertEqual(result["counts"]["observed"], 1)
        self.assertEqual(result["items"][0]["status"], "OBSERVED_SINGLE_SOURCE")

    def test_entitlement_errors_are_classified_as_unknown_access_not_pass(self):
        class ForbiddenProvider:
            configured = True

            def quote(self, instrument):
                raise DataProviderError("HTTP 403 from api.twelvedata.com: plan upgrade required")

        board = GlobalMarketBoard(
            FakeHttp({}), instruments=(self.instrument,), provider=ForbiddenProvider(), cache_ttl_seconds=60
        )
        result = board.get_board()
        deadline = time.monotonic() + 2
        while result["status"] == "LOADING" and time.monotonic() < deadline:
            time.sleep(0.01)
            result = board.get_board()

        self.assertEqual(result["items"][0]["status"], "ENTITLEMENT_REQUIRED")
        self.assertFalse(result["items"][0]["trade_eligible"])


if __name__ == "__main__":
    unittest.main()
