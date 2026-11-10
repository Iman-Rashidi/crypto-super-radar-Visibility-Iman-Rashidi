"""Offline integration tests for the dependency-light HTTP dashboard."""
from __future__ import annotations

import json
import queue
import re
import threading
import time
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

    def test_head_health_probe_succeeds_without_body(self):
        status, headers, payload = self.request("HEAD", "/")
        self.assertEqual(status, 200)
        self.assertEqual(payload, b"")
        self.assertEqual(headers.get("Cache-Control"), "no-store")

    def test_health_endpoint_is_live_and_not_cacheable(self):
        status, headers, payload = self.request("GET", "/health")
        self.assertEqual(status, 200)
        health = json.loads(payload)
        self.assertEqual(health["status"], "ok")
        self.assertIn("commit", health)
        self.assertEqual(headers.get("Cache-Control"), "no-store")

    def test_home_page_is_persian_and_lists_twelve_gates(self):
        status, headers, payload = self.request("GET", "/")
        page = payload.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn('lang="fa" dir="rtl"', page)
        self.assertIn("۱۲ فیلتر اجباری", page)
        self.assertIn("Paper Research", page)
        self.assertIn("ربات معامله‌گر آزمایشی", page)
        self.assertIn("سقف زیان روزانه", page)
        self.assertIn('action="/paper-bot/tick"', page)
        self.assertIn("python scripts/paper_trading_bot.py", page)
        self.assertIn("ماشین‌حساب حرفه‌ای سناریوی Spot", page)
        self.assertIn('id="calc-amount"', page)
        self.assertIn("calc-break-even", page)
        self.assertIn("بازار رمزارزها", page)
        self.assertIn("APIهای عمومی و رایگان", page)
        self.assertIn("/api/crypto-markets", page)
        self.assertIn('id="crypto-market-search"', page)
        self.assertIn('id="crypto-market-sort"', page)
        self.assertIn('id="crypto-market-search-activity"', page)
        self.assertIn("شیبا داره بو می‌کشه", page)
        self.assertIn("shiba-sniff-body", page)
        self.assertIn('class="market-search-dog market-search-spin"', page)
        self.assertIn("shiba-sniff-head", page)
        self.assertIn("shiba-tail-wag", page)
        self.assertIn("market-search-treasure", page)
        self.assertIn("پیداش کرد! گنج اینجاست!", page)
        self.assertIn("نمایش ", page)
        self.assertIn('id="scan-asset"', page)
        self.assertIn('id="scan-section"', page)
        self.assertIn("انتخاب برای اسکن", page)
        self.assertIn("scanButton.disabled=item.status!=='PASS'", page)
        self.assertIn("input.value='cex:'+item.symbol", page)
        self.assertIn('class="app-header"', page)
        self.assertIn("var(--teal)", page)
        self.assertLess(page.index('id="crypto-markets"'), page.index('id="scan-section"'))

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
            self.assertIn(first["status"], {"LOADING", "PASS"})
            deadline = time.monotonic() + 5
            while (first.get("status") != "PASS" or first.get("refreshing")) and time.monotonic() < deadline:
                time.sleep(0.01)
                status, _, payload = self.request("GET", "/api/crypto-markets")
                first = json.loads(payload)
            self.assertEqual(first["status"], "PASS")
            self.assertEqual(first["cache_ttl_seconds"], 60)
            self.assertEqual(len(first["items"]), len(web_app.CRYPTO_MARKET_UNIVERSE))
            self.assertGreaterEqual(len(first["items"]), 20)
            self.assertEqual(len({item["symbol"] for item in first["items"]}), len(first["items"]))
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
            deadline = time.monotonic() + 5
            while (result.get("status") != "PARTIAL" or result.get("refreshing")) and time.monotonic() < deadline:
                time.sleep(0.01)
                status, _, payload = self.request("GET", "/api/crypto-markets")
                result = json.loads(payload)
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "PARTIAL")
        eth = next(item for item in result["items"] if item["symbol"] == "ETHUSDT")
        self.assertEqual(eth["status"], "UNKNOWN")
        self.assertIn("public source unavailable", eth["reason"])

    def test_gem_hunter_endpoint_exposes_only_qualified_buy_candidates(self):
        from tests.test_gem_hunter_qualification import valid_candidate
        web_app.GEM_HUNTER_CACHE.update(expires_at=0.0, payload=None)
        good = valid_candidate()
        bad = dict(good)
        bad["candidate_status"] = "UNSCREENED"
        bad["trade_authorization"] = "NO_TRADE"
        with patch.object(web_app, "discover_gem_candidates", return_value={
            "status": "PASS", "candidate_count": 2,
            "candidates": [good, bad], "trade_authorization": "NO_TRADE",
        }):
            status, _, payload = self.request("GET", "/api/gem-hunter/discover?limit=20")
        result = json.loads(payload)
        self.assertEqual(status, 200)
        self.assertEqual(result["signal_status"], "BUY_FOUND")
        self.assertEqual(result["qualified_buy_count"], 1)
        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["candidates"][0]["trade_authorization"], "BUY")
        self.assertNotIn("available_candidate_count", result)
        self.assertNotIn("trade_authorization", result)

    def test_gem_hunter_endpoint_hides_all_unqualified_candidates(self):
        web_app.GEM_HUNTER_CACHE.update(expires_at=0.0, payload=None)
        raw = {
            "status": "PASS", "candidate_count": 1,
            "candidates": [{
                "chain": "solana", "token_address": "61iusjsjCqfCW6SViHytSyYqvALUMcv3k2xMsUtM1UwP",
                "candidate_status": "UNSCREENED", "trade_authorization": "NO_TRADE",
            }],
            "trade_authorization": "NO_TRADE",
        }
        with patch.object(web_app, "discover_gem_candidates", return_value=raw):
            status, _, payload = self.request("GET", "/api/gem-hunter/discover?limit=20")
        result = json.loads(payload)
        self.assertEqual(status, 200)
        self.assertEqual(result["signal_status"], "NO_QUALIFIED_BUY")
        self.assertEqual(result["qualified_buy_count"], 0)
        self.assertEqual(result["candidates"], [])
        self.assertNotIn("available_candidate_count", result)

    def test_paper_bot_status_is_local_and_cannot_submit_live_orders(self):
        status, _, payload = self.request("GET", "/paper-bot/status")
        result = json.loads(payload)
        self.assertEqual(status, 200)
        self.assertEqual(result["mode"], "PAPER_ONLY")
        self.assertFalse(result["live_orders_enabled"])
        self.assertFalse(result["orders_submitted"])
        self.assertEqual(result["storage"], "LOCAL_SQLITE")

    def test_history_endpoint_returns_local_sqlite_summary(self):
        status, headers, payload = self.request(
            "GET", "/history?asset=" + quote("cex:BTCUSDT", safe="")
        )
        result = json.loads(payload)
        self.assertEqual(status, 200)
        self.assertEqual(result["storage"], "LOCAL_SQLITE")
        self.assertIn("observation_count", result)

    def test_identical_active_scan_posts_are_idempotent(self):
        old_jobs = dict(web_app.JOBS)
        old_keys = dict(web_app.ACTIVE_SCAN_KEYS)
        old_queue = web_app.SCAN_QUEUE
        web_app.JOBS.clear()
        web_app.ACTIVE_SCAN_KEYS.clear()
        web_app.SCAN_QUEUE = queue.Queue(maxsize=50)
        body = "asset=cex%3ABTCUSDT&headline=market+test"
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Content-Length": str(len(body)),
        }
        try:
            with patch.object(web_app, "_ensure_scan_worker"):
                first_status, _, first_payload = self.request("POST", "/scan", body=body, headers=headers)
                second_status, _, second_payload = self.request("POST", "/scan", body=body, headers=headers)
            first_id = re.search(r"شناسه کار: <code>([a-f0-9]+)</code>", first_payload.decode("utf-8")).group(1)
            second_id = re.search(r"شناسه کار: <code>([a-f0-9]+)</code>", second_payload.decode("utf-8")).group(1)
            self.assertEqual(first_status, 202)
            self.assertEqual(second_status, 202)
            self.assertEqual(first_id, second_id)
            self.assertEqual(web_app.SCAN_QUEUE.qsize(), 1)
            job = web_app.JOBS[first_id]
            self.assertEqual(job["status"], "queued")
            self.assertIsInstance(job["fencing_token"], int)
            self.assertTrue(job["idempotency_key"])
            self.assertIn("valid_until_utc", job)
        finally:
            web_app.JOBS.clear()
            web_app.JOBS.update(old_jobs)
            web_app.ACTIVE_SCAN_KEYS.clear()
            web_app.ACTIVE_SCAN_KEYS.update(old_keys)
            web_app.SCAN_QUEUE = old_queue

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
