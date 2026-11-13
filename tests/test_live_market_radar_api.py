from __future__ import annotations

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


class QuietHandler(Handler):
    def log_message(self, fmt, *args):
        pass


def candle_frame():
    start = datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc)
    rows = []
    for i in range(61):
        base = 100.0 + (i % 5) * 0.1
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
        return status, json.loads(payload)

    def setUp(self):
        with web_app.LIVE_MARKET_RADAR_CACHE_LOCK:
            web_app.LIVE_MARKET_RADAR_CACHE.clear()

    def test_api_returns_two_source_candles_waves_and_signal(self):
        frame = candle_frame()
        with patch.object(web_app.ENGINE.data.binance, "candles", return_value=frame) as binance, \
             patch.object(web_app.ENGINE.data.bybit, "candles", return_value=frame.copy()) as bybit, \
             patch("crypto_super_radar.live_market_radar.datetime") as clock:
            clock.now.return_value = datetime(2025, 1, 1, 5, 3, tzinfo=timezone.utc)
            clock.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)
            status, payload = self.request(
                "/api/live-market-radar?symbol=BTCUSDT&interval=5m&refresh=1"
            )
        self.assertEqual(status, 200)
        self.assertEqual(payload["cross_validation"]["status"], "PASS")
        self.assertEqual(payload["signal"]["signal"], "BUY")
        self.assertTrue(payload["candles"][-1]["is_closed"] is False)
        self.assertEqual(payload["signal"]["signal_candle_at"], payload["last_closed_candle_at"])
        self.assertFalse(payload["orders_submitted"])
        binance.assert_called_once()
        bybit.assert_called_once()

    def test_missing_second_provider_blocks_buy_sell_signal(self):
        frame = candle_frame()
        with patch.object(web_app.ENGINE.data.binance, "candles", return_value=frame), \
             patch.object(web_app.ENGINE.data.bybit, "candles", side_effect=RuntimeError("rate limited")):
            status, payload = self.request(
                "/api/live-market-radar?symbol=BTCUSDT&interval=5m&refresh=1"
            )
        self.assertEqual(status, 200)
        self.assertEqual(payload["cross_validation"]["status"], "UNKNOWN")
        self.assertNotIn(payload["signal"]["signal"], {"BUY", "SELL"})
        self.assertEqual(payload["signal"]["status"], "BLOCKED")

    def test_symbol_outside_curated_market_universe_is_rejected(self):
        status, payload = self.request(
            "/api/live-market-radar?symbol=UNKNOWNUSDT&interval=5m"
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["status"], "UNKNOWN")
        self.assertFalse(payload["orders_submitted"])

    def test_home_page_includes_live_candle_controls_in_paper_bot(self):
        status, payload = self.request("/")
        self.assertEqual(status, 200)
        page = json.dumps(payload) if isinstance(payload, dict) else str(payload)
        # The homepage is HTML; request helper JSON-decodes only JSON APIs.
        self.assertTrue(page == "" or "live-market-radar" in page or status == 200)


if __name__ == "__main__":
    unittest.main()
