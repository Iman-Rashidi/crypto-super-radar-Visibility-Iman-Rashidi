"""Offline integration tests for the dependency-light HTTP dashboard."""
from __future__ import annotations

import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from http.client import HTTPConnection
from urllib.parse import quote

from crypto_super_radar.web_app import Handler
from crypto_super_radar import web_app


class QuietHandler(Handler):
    def log_message(self, fmt, *args):
        pass


class WebAppHttpTests(unittest.TestCase):
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

    def request(self, method: str, path: str, body=None, headers=None):
        connection = HTTPConnection(self.host, self.port, timeout=3)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        payload = response.read()
        status = response.status
        response_headers = dict(response.getheaders())
        connection.close()
        return status, response_headers, payload

    def test_health_endpoint_is_live_and_not_cacheable(self):
        status, headers, payload = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload), {"status": "ok"})
        self.assertEqual(headers.get("Cache-Control"), "no-store")

    def test_home_page_is_persian_and_lists_twelve_gates(self):
        status, headers, payload = self.request("GET", "/")
        page = payload.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn('lang="fa" dir="rtl"', page)
        self.assertIn("۱۲ فیلتر اجباری", page)
        self.assertIn("Paper Research", page)
        self.assertIn("ماشین‌حساب حرفه‌ای سناریوی Spot", page)
        self.assertIn('id="calc-amount"', page)
        self.assertIn("calc-break-even", page)
        self.assertIn("بازار رمزارزها", page)
        self.assertIn("APIهای عمومی و رایگان", page)
        self.assertIn("/api/crypto-markets", page)
        self.assertIn('id="scan-asset"', page)
        self.assertIn('id="scan-section"', page)
        self.assertIn("انتخاب برای اسکن", page)
        self.assertIn("scanButton.disabled=item.status!=='PASS'", page)
        self.assertIn("input.value='cex:'+item.symbol", page)

    def test_crypto_market_endpoint_uses_free_cross_validated_spot_data_and_cache(self):
        web_app.CRYPTO_MARKET_CACHE.update(expires_at=0.0, payload=None)

        def snapshot(asset):
            return SimpleNamespace(
                price_usd=100.0,
                volume_24h_usd=1_000_000.0,
                price_discrepancy_pct=0.25,
                provider_a="Binance Spot",
                provider_b="Bybit Spot",
            )

        with patch.object(web_app.ENGINE.data, "snapshot", side_effect=snapshot) as fetch:
            status, _, payload = self.request("GET", "/api/crypto-markets")
            first = json.loads(payload)
            self.assertEqual(status, 200)
            self.assertEqual(first["status"], "PASS")
            self.assertEqual(first["cache_ttl_seconds"], 60)
            self.assertEqual(len(first["items"]), len(web_app.CRYPTO_MARKET_UNIVERSE))
            self.assertEqual(first["items"][0]["symbol"], "BTCUSDT")
            self.assertEqual(first["items"][0]["providers"], ["Binance Spot", "Bybit Spot"])

            status, _, payload = self.request("GET", "/api/crypto-markets")
            second = json.loads(payload)
            self.assertEqual(status, 200)
            self.assertEqual(second, first)
            self.assertEqual(fetch.call_count, len(web_app.CRYPTO_MARKET_UNIVERSE))

    def test_crypto_market_endpoint_keeps_failed_symbols_unknown(self):
        web_app.CRYPTO_MARKET_CACHE.update(expires_at=0.0, payload=None)

        def snapshot(asset):
            if asset.symbol == "ETHUSDT":
                raise RuntimeError("public source unavailable")
            return SimpleNamespace(
                price_usd=100.0,
                volume_24h_usd=1_000_000.0,
                price_discrepancy_pct=0.25,
                provider_a="Binance Spot",
                provider_b="Bybit Spot",
            )

        with patch.object(web_app.ENGINE.data, "snapshot", side_effect=snapshot):
            status, _, payload = self.request("GET", "/api/crypto-markets")
        result = json.loads(payload)
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "PARTIAL")
        eth = next(item for item in result["items"] if item["symbol"] == "ETHUSDT")
        self.assertEqual(eth["status"], "UNKNOWN")
        self.assertIn("public source unavailable", eth["reason"])

    def test_history_endpoint_returns_local_sqlite_summary(self):
        status, headers, payload = self.request(
            "GET", "/history?asset=" + quote("cex:BTCUSDT", safe="")
        )
        result = json.loads(payload)
        self.assertEqual(status, 200)
        self.assertEqual(result["storage"], "LOCAL_SQLITE")
        self.assertIn("observation_count", result)

    def test_unknown_job_returns_not_found(self):
        status, _, payload = self.request("GET", "/jobs/not-a-real-job")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(payload), {"error": "job not found"})

    def test_cross_origin_scan_post_is_rejected_before_engine_execution(self):
        status, _, payload = self.request(
            "POST",
            "/scan",
            body="asset=cex%3ABTCUSDT",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Content-Length": str(len("asset=cex%3ABTCUSDT")),
                "Origin": "https://attacker.example",
            },
        )
        self.assertEqual(status, 403)
        self.assertIn(b"Cross-origin", payload)


if __name__ == "__main__":
    unittest.main()
