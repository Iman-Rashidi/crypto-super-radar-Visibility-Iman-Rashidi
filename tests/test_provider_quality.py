from __future__ import annotations

import time
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import MagicMock, patch

import pandas as pd

from crypto_super_radar.config import AppConfig
from crypto_super_radar.providers import (
    BinanceProvider,
    BybitProvider,
    DataProviderError,
    PerpetualMarketProvider,
    PublicHttpClient,
    _finite_float,
    _ohlcv_frame,
)


class NumericNormalizationTests(unittest.TestCase):
    def test_signed_funding_is_finite_and_preserved(self):
        self.assertEqual(_finite_float("-0.0003"), -0.0003)
        self.assertEqual(_finite_float("0.0002"), 0.0002)
        self.assertIsNone(_finite_float("nan"))
        self.assertIsNone(_finite_float("inf"))
        self.assertIsNone(_finite_float(None))
        self.assertIsNone(_finite_float(True))

    def test_ohlcv_accepts_valid_rows_and_sorts_timestamps(self):
        now = int(time.time())
        rows = [
            [now - 60, "11", "13", "10", "12", "100"],
            [now - 120, "10", "12", "9", "11", "90"],
        ]
        frame = _ohlcv_frame(rows)
        self.assertEqual(list(frame.columns), ["open", "high", "low", "close", "volume"])
        self.assertEqual(len(frame), 2)
        self.assertTrue(frame.index.is_monotonic_increasing)

    def test_ohlcv_rejects_duplicate_timestamp(self):
        now = int(time.time()) - 60
        row = [now, "10", "12", "9", "11", "90"]
        with self.assertRaisesRegex(DataProviderError, "Duplicate OHLCV timestamp"):
            _ohlcv_frame([row, row])

    def test_ohlcv_rejects_invalid_high_low_relationship(self):
        now = int(time.time()) - 60
        with self.assertRaisesRegex(DataProviderError, "Inconsistent OHLC range"):
            _ohlcv_frame([[now, "10", "9", "8", "10", "90"]])

    def test_ohlcv_rejects_future_candle(self):
        future = int(time.time()) + 600
        with self.assertRaisesRegex(DataProviderError, "Future OHLCV timestamp"):
            _ohlcv_frame([[future, "10", "12", "9", "11", "90"]])

    def test_ohlcv_rejects_fractional_timestamp(self):
        timestamp = int(time.time()) - 60 + 0.5
        with self.assertRaisesRegex(DataProviderError, "Fractional OHLCV timestamp"):
            _ohlcv_frame([[timestamp, "10", "12", "9", "11", "90"]])

    def test_ohlcv_rejects_non_finite_or_negative_price(self):
        now = int(time.time()) - 60
        with self.assertRaisesRegex(DataProviderError, "Missing or non-finite OHLCV"):
            _ohlcv_frame([[now, "10", "12", "9", "-1", "90"]])


class HttpRetryTests(unittest.TestCase):
    def test_retry_after_accepts_seconds_and_http_date(self):
        self.assertEqual(PublicHttpClient._retry_after_seconds("4", 0), 4.0)
        future = datetime.now(timezone.utc) + timedelta(seconds=7)
        parsed = PublicHttpClient._retry_after_seconds(format_datetime(future, usegmt=True), 0)
        self.assertGreaterEqual(parsed, 0.0)
        self.assertLessEqual(parsed, 30.0)
        self.assertEqual(PublicHttpClient._retry_after_seconds("not-a-date", 1), 2.0)
        self.assertEqual(PublicHttpClient._retry_after_seconds("999", 0), 30.0)

    @patch("crypto_super_radar.providers.time.sleep")
    @patch("crypto_super_radar.providers.requests.request")
    def test_rate_limit_retries_and_then_returns_json(self, request, sleep):
        first = MagicMock(status_code=429, headers={"Retry-After": "0"}, text="rate limited")
        second = MagicMock(status_code=200, headers={}, text="")
        second.json.return_value = {"ok": True}
        request.side_effect = [first, second]

        client = PublicHttpClient(AppConfig(max_http_retries=2))
        client.limiter.wait = MagicMock()
        self.assertEqual(client.get("https://api.example.test/data"), {"ok": True})
        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(0.0)

    @patch("crypto_super_radar.providers.requests.request")
    def test_non_retryable_http_error_fails_immediately(self, request):
        response = MagicMock(status_code=400, headers={}, text="bad request")
        request.return_value = response
        client = PublicHttpClient(AppConfig(max_http_retries=3))
        client.limiter.wait = MagicMock()
        with self.assertRaisesRegex(DataProviderError, "HTTP 400"):
            client.get("https://api.example.test/data")
        self.assertEqual(request.call_count, 1)


    @patch("crypto_super_radar.providers.requests.request")
    def test_geckoterminal_429_is_not_retried_inside_same_scan(self, request):
        response = MagicMock(status_code=429, headers={"Retry-After": "30"}, text="rate limited")
        request.return_value = response
        client = PublicHttpClient(AppConfig(max_http_retries=3))
        client.limiter.wait = MagicMock()
        with self.assertRaisesRegex(DataProviderError, "HTTP 429"):
            client.get("https://api.geckoterminal.com/api/v2/networks/base/new_pools")
        self.assertEqual(request.call_count, 1)


class PerpetualProviderTests(unittest.TestCase):
    def test_binance_ticker_preserves_negative_funding(self):
        fake_http = MagicMock()
        fake_http.get.side_effect = [
            {"price": "65000"},
            {"quoteVolume": "1000000000"},
            {"lastFundingRate": "-0.000125"},
            {"openInterest": "12345"},
        ]
        result = PerpetualMarketProvider(fake_http).binance_ticker("BTCUSDT")
        self.assertAlmostEqual(result["funding_rate"], -0.000125)

    def test_bybit_ticker_rejects_nonzero_retcode(self):
        fake_http = MagicMock()
        fake_http.get.return_value = {
            "retCode": 10001,
            "retMsg": "parameter error",
            "result": {"list": []},
        }
        with self.assertRaisesRegex(DataProviderError, "API error"):
            PerpetualMarketProvider(fake_http).bybit_ticker("BTCUSDT")

    def test_spot_price_must_be_strictly_positive(self):
        fake_http = MagicMock()
        fake_http.get.side_effect = [
            {"price": "0"},
            {"quoteVolume": "1000"},
        ]
        with self.assertRaisesRegex(DataProviderError, "incomplete ticker"):
            BinanceProvider(fake_http).ticker("BTCUSDT")

    def test_bybit_spot_checks_api_return_code(self):
        fake_http = MagicMock()
        fake_http.get.return_value = {
            "retCode": 10001,
            "retMsg": "parameter error",
            "result": {"list": [{"lastPrice": "10", "turnover24h": "100"}]},
        }
        with self.assertRaisesRegex(DataProviderError, "API error"):
            BybitProvider(fake_http).ticker("BTCUSDT")

    def test_funding_intervals_default_to_binance_eight_hours_and_match_bybit(self):
        fake_http = MagicMock()
        fake_http.get.side_effect = [
            [],
            {"retCode": 0, "result": {"list": [{"symbol": "BTCUSDT", "fundingInterval": "480"}]}},
        ]
        result = PerpetualMarketProvider(fake_http).funding_interval_hours("BTCUSDT")
        self.assertEqual(result, 8.0)

    def test_adjusted_funding_interval_must_match_both_venues(self):
        fake_http = MagicMock()
        fake_http.get.side_effect = [
            [{"symbol": "BTCUSDT", "fundingIntervalHours": 4}],
            {"retCode": 0, "result": {"list": [{"symbol": "BTCUSDT", "fundingInterval": "240"}]}},
        ]
        self.assertEqual(PerpetualMarketProvider(fake_http).funding_interval_hours("BTCUSDT"), 4.0)

    def test_funding_interval_mismatch_fails_closed(self):
        fake_http = MagicMock()
        fake_http.get.side_effect = [
            [{"symbol": "BTCUSDT", "fundingIntervalHours": 4}],
            {"retCode": 0, "result": {"list": [{"symbol": "BTCUSDT", "fundingInterval": "480"}]}},
        ]
        with self.assertRaisesRegex(DataProviderError, "interval mismatch"):
            PerpetualMarketProvider(fake_http).funding_interval_hours("BTCUSDT")

    def test_missing_bybit_funding_interval_is_unknown(self):
        fake_http = MagicMock()
        fake_http.get.side_effect = [
            [],
            {"retCode": 0, "result": {"list": [{"symbol": "BTCUSDT"}]}},
        ]
        with self.assertRaisesRegex(DataProviderError, "funding interval"):
            PerpetualMarketProvider(fake_http).funding_interval_hours("BTCUSDT")

    def test_historical_funding_matches_both_venues_and_keeps_negative_rates(self):
        now = pd.Timestamp.now(tz="UTC").floor("8h")
        stamps = [now - pd.Timedelta(hours=16), now - pd.Timedelta(hours=8), now]
        rates = [-0.0002, 0.0001, -0.0003]
        binance_rows = [
            {"fundingTime": int(ts.timestamp() * 1000), "fundingRate": str(rate)}
            for ts, rate in zip(stamps, rates)
        ]
        bybit_rows = [
            {"fundingRateTimestamp": str(int(ts.timestamp() * 1000)), "fundingRate": str(rate)}
            for ts, rate in zip(stamps, rates)
        ]
        fake_http = MagicMock()
        fake_http.get.side_effect = [
            [],
            {"retCode": 0, "result": {"list": [{"symbol": "BTCUSDT", "fundingInterval": "480"}]}},
            binance_rows,
            {"retCode": 0, "result": {"list": bybit_rows}},
        ]

        result = PerpetualMarketProvider(fake_http).funding_history("BTCUSDT")
        self.assertEqual(len(result), 3)
        self.assertAlmostEqual(float(result.iloc[0]), -0.0002)
        self.assertAlmostEqual(float(result.iloc[-1]), -0.0003)

    def test_historical_funding_rejects_cross_venue_disagreement(self):
        now = pd.Timestamp.now(tz="UTC").floor("8h")
        stamps = [now - pd.Timedelta(hours=16), now - pd.Timedelta(hours=8), now]
        a_rates = [-0.0002, 0.0001, -0.0003]
        b_rates = [-0.0002, 0.0001, 0.0010]
        a = [{"fundingTime": int(ts.timestamp() * 1000), "fundingRate": str(rate)}
             for ts, rate in zip(stamps, a_rates)]
        b = [{"fundingRateTimestamp": str(int(ts.timestamp() * 1000)), "fundingRate": str(rate)}
             for ts, rate in zip(stamps, b_rates)]
        fake_http = MagicMock()
        fake_http.get.side_effect = [
            [],
            {"retCode": 0, "result": {"list": [{"symbol": "BTCUSDT", "fundingInterval": "480"}]}},
            a,
            {"retCode": 0, "result": {"list": b}},
        ]
        with self.assertRaisesRegex(DataProviderError, "diverge"):
            PerpetualMarketProvider(fake_http).funding_history("BTCUSDT")


class FreeRegionalFallbackTests(unittest.TestCase):
    def test_bybit_spot_access_denial_uses_public_okx_ticker(self):
        http = MagicMock()
        http.get.side_effect = [
            DataProviderError("HTTP 403 from api.bybit.com"),
            {"code": "0", "data": [{"last": "65000", "volCcy24h": "1200000"}]},
        ]
        result = BybitProvider(http).ticker("BTCUSDT")
        self.assertEqual(result["provider"], "OKX Spot")
        self.assertEqual(result["price"], 65000)
        self.assertEqual(http.get.call_count, 2)

    def test_binance_perpetual_access_denial_uses_public_gate_ticker(self):
        http = MagicMock()
        http.get.side_effect = [
            DataProviderError("HTTP 451 from fapi.binance.com"),
            [{"mark_price": "65000", "volume_24h_settle": "1000000",
              "funding_rate": "-0.0001", "total_size": "1234"}],
            {"funding_interval": 28800},
        ]
        result = PerpetualMarketProvider(http).binance_ticker("BTCUSDT")
        self.assertEqual(result["provider"], "Gate.io USDT Perpetual")
        self.assertEqual(result["funding_rate"], -0.0001)
        self.assertEqual(result["funding_interval_hours"], 8)

    def test_bybit_perpetual_access_denial_uses_public_okx_swap(self):
        http = MagicMock()
        http.get.side_effect = [
            DataProviderError("HTTP 403 from api.bybit.com"),
            {"code": "0", "data": [{"last": "65000", "volCcy24h": "1000000"}]},
            {"code": "0", "data": [{"fundingRate": "-0.0002"}]},
            {"code": "0", "data": [{"oi": "4321"}]},
        ]
        result = PerpetualMarketProvider(http).bybit_ticker("BTCUSDT")
        self.assertEqual(result["provider"], "OKX USDT Swap")
        self.assertEqual(result["funding_rate"], -0.0002)
        self.assertEqual(result["open_interest"], 4321)


if __name__ == "__main__":
    unittest.main()
