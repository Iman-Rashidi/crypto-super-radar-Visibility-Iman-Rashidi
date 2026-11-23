from __future__ import annotations

import atexit
import json
import threading
import unittest
from datetime import datetime, timezone
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import pandas as pd

from crypto_super_radar import web_app
from crypto_super_radar.web_app import Handler


def _close_web_app_test_database_connections():
    resources = (
        web_app.ENGINE.data,
        web_app.ENGINE.whale_store,
        web_app.PAPER_BOT.ledger,
        web_app.FUTURES_PAPER_BOT.ledger,
    )
    for resource in resources:
        close = getattr(resource, "close", None)
        if callable(close):
            close()


atexit.register(_close_web_app_test_database_connections)


class QuietHandler(Handler):
    def log_message(self, fmt, *args):
        pass


def candle_frame():
    start = datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc)
    rows = []
    for i in range(61):
        base = 100.0
        opened, high, low, close = base, base + 1.0, base - 1.0, base + 0.2
        if i == 50:
            opened, high, low, close = 106.0, 110.0, 105.0, 108.0
        if i == 55:
            opened, high, low, close = 92.0, 94.0, 90.0, 91.0
        if i in (56, 57, 58):
            opened, high, low, close = 92.0, 94.0 + (i - 56) * 0.2, 91.5, 93.0
        if i == 59:
            opened, high, low, close = 93.0, 96.0, 92.5, 95.5
        if i == 60:
            opened, high, low, close = 95.5, 97.0, 95.0, 96.5
        rows.append({"open": opened, "high": high, "low": low, "close": close, "volume": 1000.0})
    return pd.DataFrame(rows, index=pd.date_range(start=start, periods=61, freq="5min"))


class LiveMarketRadarApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.host, cls.port = cls.server.server_address

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def request(self, path):
        connection = HTTPConnection(self.host, self.port, timeout=5)
        connection.request("GET", path)
        response = connection.getresponse()
        status = response.status
        payload = response.read()
        connection.close()
        return status, payload

    def setUp(self):
        with web_app.LIVE_MARKET_RADAR_CACHE_LOCK:
            web_app.LIVE_MARKET_RADAR_CACHE.clear()

    @staticmethod
    def provider_rows(frame, timestamp_scale=1000):
        return [
            [int(stamp.timestamp() * (1000 if timestamp_scale == 1000 else 1)),
             str(row.open), str(row.high), str(row.low), str(row.close), str(row.volume)]
            for stamp, row in frame.iterrows()
        ]

    def test_api_returns_two_source_candles_waves_and_signal(self):
        frame = candle_frame()
        rows = self.provider_rows(frame)
        def get_json(url, params=None, timeout=1.35):
            if "data-api.binance.vision" in url:
                return rows
            if "api.bybit.com" in url:
                return {"retCode": 0, "result": {"list": rows}}
            raise RuntimeError("fallback feed deliberately unavailable in this test")
        with patch.object(web_app.ENGINE.data.http, "get_with_timeout", side_effect=get_json), \
             patch("crypto_super_radar.live_market_radar.datetime") as clock:
            clock.now.return_value = datetime(2025, 1, 1, 5, 3, tzinfo=timezone.utc)
            clock.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)
            status, raw_payload = self.request(
                "/api/live-market-radar?symbol=BTCUSDT&interval=5m&refresh=1"
            )
        payload = json.loads(raw_payload)
        self.assertEqual(status, 200)
        self.assertEqual(payload["cross_validation"]["status"], "PASS")
        self.assertEqual(payload["signal"]["signal"], "BUY")
        self.assertTrue(payload["candles"][-1]["is_closed"] is False)
        self.assertEqual(payload["signal"]["signal_candle_at"], payload["last_closed_candle_at"])
        self.assertFalse(payload["orders_submitted"])
        self.assertEqual(payload["cross_validation"]["providers"], ["Binance Spot", "Bybit Spot"])

    def test_missing_second_provider_blocks_buy_sell_signal(self):
        frame = candle_frame()
        rows = self.provider_rows(frame)
        def get_json(url, params=None, timeout=1.35):
            if "data-api.binance.vision" in url:
                return rows
            raise RuntimeError("rate limited")
        with patch.object(web_app.ENGINE.data.http, "get_with_timeout", side_effect=get_json), \
             patch("crypto_super_radar.live_market_radar.datetime") as clock:
            clock.now.return_value = datetime(2025, 1, 1, 5, 3, tzinfo=timezone.utc)
            clock.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)
            status, raw_payload = self.request(
                "/api/live-market-radar?symbol=BTCUSDT&interval=5m&refresh=1"
            )
        payload = json.loads(raw_payload)
        self.assertEqual(status, 200)
        self.assertEqual(payload["cross_validation"]["status"], "UNKNOWN")
        self.assertNotIn(payload["signal"]["signal"], {"BUY", "SELL"})
        self.assertEqual(payload["signal"]["status"], "BLOCKED")

    def test_api_falls_back_to_okx_and_kraken_when_binance_and_bybit_fail(self):
        frame = candle_frame()
        okx_rows = [
            [str(row[0]), row[1], row[2], row[3], row[4], row[5], row[5], row[5], "1"]
            for row in self.provider_rows(frame)
        ]
        kraken_rows = [
            [int(row[0] / 1000), row[1], row[2], row[3], row[4], "101.0", row[5], 10]
            for row in self.provider_rows(frame)
        ]
        def get_json(url, params=None, timeout=1.35):
            if "data-api.binance.vision" in url or "api.bybit.com" in url:
                raise RuntimeError("HTTP 418 / region unavailable")
            if "www.okx.com" in url:
                return {"code": "0", "data": okx_rows}
            if "api.kraken.com" in url:
                return {"error": [], "result": {"XBTUSDT": kraken_rows, "last": kraken_rows[-1][0]}}
            raise AssertionError(url)
        with patch.object(web_app.ENGINE.data.http, "get_with_timeout", side_effect=get_json), \
             patch("crypto_super_radar.live_market_radar.datetime") as clock:
            clock.now.return_value = datetime(2025, 1, 1, 5, 3, tzinfo=timezone.utc)
            clock.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)
            status, raw_payload = self.request(
                "/api/live-market-radar?symbol=BTCUSDT&interval=5m&refresh=1"
            )
        payload = json.loads(raw_payload)
        self.assertEqual(status, 200)
        self.assertEqual(payload["feed_diagnostics"]["primary_provider"], "OKX Spot")
        self.assertEqual(payload["feed_diagnostics"]["secondary_provider"], "Kraken Spot")
        self.assertTrue(payload["feed_diagnostics"]["fallback_used"])
        self.assertGreaterEqual(len(payload["candles"]), 35)
        self.assertEqual(payload["cross_validation"]["status"], "PASS")
        self.assertFalse(payload["orders_submitted"])

    def test_valid_custom_symbol_is_normalized_and_sent_to_spot_analyzer(self):
        expected = {"symbol": "UNKNOWNUSDT", "status": "OK", "candles": [], "orders_submitted": False}
        with patch.object(web_app, "_live_market_radar_payload", return_value=expected) as builder:
            status, raw_payload = self.request(
                "/api/live-market-radar?symbol=unknown&interval=5m"
            )
        payload = json.loads(raw_payload)
        self.assertEqual(status, 200)
        self.assertEqual(payload["symbol"], "UNKNOWNUSDT")
        builder.assert_called_once_with("UNKNOWNUSDT", "5m", force_refresh=False)

    def test_home_page_includes_live_candle_controls_in_paper_bot(self):
        connection = HTTPConnection(self.host, self.port, timeout=5)
        connection.request("GET", "/")
        response = connection.getresponse()
        status = response.status
        page = response.read().decode("utf-8")
        connection.close()
        self.assertEqual(status, 200)
        self.assertIn('id="live-market-radar"', page)
        self.assertIn('id="live-candle-chart"', page)
        self.assertIn('id="live-radar-custom-symbol"', page)
        self.assertIn('id="live-radar-custom-search"', page)
        self.assertIn('id="live-wave-watch-grid"', page)
        for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "LINKUSDT"):
            self.assertIn(symbol, page)
        self.assertIn("refreshWaveWatchlist", page)
        self.assertIn("نمایش روی نمودار بزرگ", page)
        self.assertIn('/api/live-market-radar', page)


if __name__ == "__main__":
    unittest.main()
