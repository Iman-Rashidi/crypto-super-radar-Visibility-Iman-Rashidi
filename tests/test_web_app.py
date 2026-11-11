"""Offline integration tests for the dependency-light HTTP dashboard."""
from __future__ import annotations

import json
from datetime import datetime, timezone
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
        self.assertIn("فقط BUYهایی نمایش داده می‌شوند", page)
        self.assertIn("کشف‌شده:", page)
        self.assertIn("وضعیت منابع داده", page)
        self.assertIn("Paper Research", page)
        self.assertIn('id="gem-hunter-watchlist"', page)
        self.assertIn('id="watchlist-manual-form"', page)
        self.assertIn('id="exchange-shortcuts"', page)
        self.assertIn('cdn.simpleicons.org/binance', page)
        self.assertIn('cdn.simpleicons.org/bybit', page)
        self.assertIn('cdn.simpleicons.org/okx', page)
        self.assertIn('/api/gem-hunter/report?limit=50', page)
        self.assertIn('crypto-super-radar.gem-hunter-watchlist.v1', page)
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

    def test_market_board_lists_actual_cross_validated_provider_names(self):
        board = web_app._market_board_payload(
            [
                {"symbol": "GRAMUSDT", "status": "PASS",
                 "providers": ["Binance Spot", "Gate.io Spot"]},
                {"symbol": "BTCUSDT", "status": "PASS",
                 "providers": ["Binance Spot", "Bybit Spot"]},
            ],
            refreshing=False,
        )
        self.assertEqual(
            board["sources"],
            ["Binance Spot", "Gate.io Spot", "Bybit Spot"],
        )

    def test_market_board_uses_gram_after_ton_exchange_rebrand(self):
        symbols = [item[0] for item in web_app.CRYPTO_MARKET_UNIVERSE]
        self.assertIn("GRAMUSDT", symbols)
        self.assertNotIn("TONUSDT", symbols)
        self.assertEqual(web_app.ASSET_NAME_ALIASES["TONCOIN"], "GRAMUSDT")
        self.assertEqual(web_app.ASSET_NAME_ALIASES["GRAM"], "GRAMUSDT")

    def test_stale_daily_candles_are_unknown_even_when_hourly_ranges_are_fresh(self):
        gate = SimpleNamespace(
            gate_id=1, status=web_app.GateStatus.PASS,
            reason_fa="کندل‌ها تازه‌اند.", reason_en="Candles are fresh.",
            evidence={"candle_age_hours": 47.7, "volume_discrepancy_pct": 0.2},
        )
        result = SimpleNamespace(gates=[gate], signal="LONG", summary_fa="قابل معامله")
        ranges = {"status": "KNOWN", "candle_age_hours": 47.7}
        timeframes = {"1h": {"status": "KNOWN", "age_hours": 1.72}}

        web_app._enforce_daily_candle_freshness(result, ranges, timeframes)

        self.assertEqual(gate.status, web_app.GateStatus.UNKNOWN)
        self.assertEqual(result.signal, web_app.Signal.NO_TRADE)
        self.assertIn("سن کندل روزانه", result.summary_fa)
        self.assertEqual(ranges["status"], "UNKNOWN")
        self.assertIn("47.7", gate.reason_fa)
        self.assertIn("36.0", gate.reason_fa)
        self.assertIn("1.7", gate.reason_fa)
        self.assertEqual(gate.evidence["daily_freshness_status"], "UNKNOWN")

    def test_fresh_daily_candles_keep_data_gate_status(self):
        gate = SimpleNamespace(
            gate_id=1, status=web_app.GateStatus.PASS,
            reason_fa="کندل‌ها تازه‌اند.", reason_en="Candles are fresh.",
            evidence={"volume_discrepancy_pct": 0.2},
        )
        result = SimpleNamespace(gates=[gate], signal="LONG", summary_fa="قابل معامله")
        ranges = {"status": "KNOWN", "candle_age_hours": 20.0}
        web_app._enforce_daily_candle_freshness(result, ranges, {"1h": {"age_hours": 1.0}})
        self.assertEqual(gate.status, web_app.GateStatus.PASS)
        self.assertEqual(result.signal, "LONG")
        self.assertEqual(ranges["status"], "KNOWN")

    def test_missing_independent_volume_comparison_blocks_data_quality(self):
        gate = SimpleNamespace(
            gate_id=1, status=web_app.GateStatus.PASS,
            reason_fa="دو منبع قیمت و حجم را تأیید کرده‌اند.",
            reason_en="Price and volume confirmed.",
            evidence={"volume_discrepancy_pct": None},
        )
        result = SimpleNamespace(gates=[gate], signal="LONG", summary_fa="قابل معامله")
        web_app._enforce_volume_cross_validation(result)
        self.assertEqual(gate.status, web_app.GateStatus.UNKNOWN)
        self.assertEqual(result.signal, web_app.Signal.NO_TRADE)
        self.assertEqual(gate.evidence["volume_cross_validation_status"], "UNKNOWN")
        self.assertIn("اختلاف حجم مستقل", gate.reason_fa)

    def test_gem_hunter_endpoint_returns_loading_without_waiting_for_providers(self):
        web_app.GEM_HUNTER_RESULT_CACHE.update(
            expires_at=0.0, payload=None, refreshing=False, last_error=None,
        )

        def mark_refreshing():
            with web_app.GEM_HUNTER_RESULT_CACHE_LOCK:
                web_app.GEM_HUNTER_RESULT_CACHE["refreshing"] = True

        with patch.object(web_app, "_start_gem_hunter_refresh", side_effect=mark_refreshing):
            status, _, payload = self.request("GET", "/api/gem-hunter/discover?limit=20")
        result = json.loads(payload)
        self.assertEqual(status, 200)
        self.assertTrue(result["refreshing"])
        self.assertEqual(result["signal_status"], "REFRESHING")
        self.assertEqual(result["candidate_count"], 0)
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["qualified_buy_count"], 0)

    def test_gem_hunter_never_serves_expired_buy_evidence_during_refresh(self):
        web_app.GEM_HUNTER_RESULT_CACHE.update(
            expires_at=0.0,
            payload={
                "status": "PASS",
                "signal_status": "BUY_FOUND",
                "qualification_engine_status": "COMPLETE",
                "candidates": [{"trade_authorization": "BUY"}],
                "candidate_count": 1,
                "qualified_buy_count": 1,
                "diagnostic_candidates": [
                    {"trade_authorization": "BUY"},
                    {"trade_authorization": "NO_TRADE", "token_name": "Old Research Token", "token_symbol": "OLD"},
                ],
                "public_policy": {"discovery_candidates_exposed": True},
            },
            refreshing=False,
            last_error=None,
        )

        def mark_refreshing():
            with web_app.GEM_HUNTER_RESULT_CACHE_LOCK:
                web_app.GEM_HUNTER_RESULT_CACHE["refreshing"] = True

        with patch.object(web_app, "_start_gem_hunter_refresh", side_effect=mark_refreshing):
            status, _, payload = self.request("GET", "/api/gem-hunter/report?limit=50")
        result = json.loads(payload)
        self.assertEqual(status, 200)
        self.assertTrue(result["refreshing"])
        self.assertTrue(result["stale"])
        self.assertEqual(result["signal_status"], "REFRESHING")
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["candidate_count"], 0)
        self.assertEqual(result["qualified_buy_count"], 0)
        self.assertEqual(result["diagnostic_freshness"], "STALE_LAST_COMPLETED_SCAN")
        self.assertEqual(len(result["diagnostic_candidates"]), 1)
        self.assertEqual(result["diagnostic_candidates"][0]["token_symbol"], "OLD")
        self.assertNotIn("BUY", [item.get("trade_authorization") for item in result["diagnostic_candidates"]])

    def test_gem_hunter_current_discovery_progress_is_visible_during_validation(self):
        web_app.GEM_HUNTER_RESULT_CACHE.update(
            expires_at=0.0,
            payload={
                "status": "PARTIAL",
                "progress_stage": "DISCOVERY_COMPLETE",
                "diagnostic_freshness": "CURRENT_SCAN_DISCOVERY",
                "diagnostic_fa": "یک نامزد پیدا شد؛ اعتبارسنجی ادامه دارد.",
                "refreshing": True,
                "stale": False,
                "candidates": [{"trade_authorization": "BUY"}],
                "candidate_count": 1,
                "qualified_buy_count": 1,
                "discovered_candidate_count": 1,
                "diagnostic_candidates": [
                    {"trade_authorization": "NO_TRADE", "token_name": "Fresh Token", "token_symbol": "FRESH"}
                ],
                "public_policy": {"discovery_candidates_exposed": True},
            },
            refreshing=True,
            last_error=None,
        )
        status, _, payload = self.request("GET", "/api/gem-hunter/report?limit=50")
        result = json.loads(payload)
        self.assertEqual(status, 200)
        self.assertTrue(result["refreshing"])
        self.assertFalse(result["stale"])
        self.assertEqual(result["diagnostic_freshness"], "CURRENT_SCAN_DISCOVERY")
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["candidate_count"], 0)
        self.assertEqual(result["qualified_buy_count"], 0)
        self.assertEqual(result["diagnostic_candidates"][0]["token_symbol"], "FRESH")

    def test_gem_hunter_discovery_publishes_candidates_before_slow_validation(self):
        web_app.GEM_HUNTER_CACHE.update(expires_at=0.0, payload=None)
        raw = {
            "status": "PARTIAL",
            "candidate_count": 1,
            "generated_at_utc": "2026-11-11T20:00:00Z",
            "sources": {"GECKOTERMINAL_NEW_POOLS_SOLANA": {"status": "PASS"}},
            "candidates": [{
                "chain": "solana",
                "token_address": "61iusjsjCqfCW6SViHytSyYqvALUMcv3k2xMsUtM1UwP",
                "token_name": "Progress Token",
                "token_symbol": "PROG",
                "candidate_status": "UNSCREENED",
                "trade_authorization": "NO_TRADE",
            }],
        }
        progress = []

        def reconcile(rows, http):
            self.assertEqual(len(progress), 1)
            self.assertEqual(progress[0]["progress_stage"], "DISCOVERY_COMPLETE")
            return rows

        with patch.object(web_app, "discover_gem_candidates", return_value=raw), \
             patch.object(web_app, "reconcile_discovery_candidates", side_effect=reconcile), \
             patch.object(web_app, "screen_discovery_candidates", side_effect=lambda rows, data: rows), \
             patch.object(web_app, "evaluate_discovery_candidates", side_effect=lambda rows: rows), \
             patch.object(web_app, "public_buy_candidates", return_value=[]):
            result = web_app._gem_hunter_discovery_payload(
                limit=50,
                include_diagnostics=True,
                progress_callback=lambda payload: progress.append(payload),
            )
        self.assertEqual(progress[0]["discovered_candidate_count"], 1)
        self.assertEqual(progress[0]["diagnostic_candidates"][0]["token_symbol"], "PROG")
        self.assertTrue(progress[0]["refreshing"])
        self.assertEqual(progress[0]["qualified_buy_count"], 0)
        self.assertEqual(result["progress_stage"], "COMPLETE")

    def test_gem_hunter_endpoint_exposes_only_qualified_buy_candidates(self):
        from test_gem_hunter_qualification import valid_candidate
        web_app.GEM_HUNTER_CACHE.update(expires_at=0.0, payload=None)
        web_app.GEM_HUNTER_RESULT_CACHE.update(
            expires_at=0.0, payload=None, refreshing=False, last_error=None,
        )
        good = valid_candidate(now=datetime.now(timezone.utc))
        bad = dict(good)
        bad["candidate_status"] = "UNSCREENED"
        bad["trade_authorization"] = "NO_TRADE"
        with patch.object(web_app, "discover_gem_candidates", return_value={
            "status": "PASS", "candidate_count": 2,
            "candidates": [good, bad], "trade_authorization": "NO_TRADE",
        }), patch.object(web_app, "reconcile_discovery_candidates", side_effect=lambda rows, http: rows), \
             patch.object(web_app, "screen_discovery_candidates", side_effect=lambda rows, data: rows), \
             patch.object(web_app, "_start_gem_hunter_refresh", side_effect=web_app._refresh_gem_hunter_result):
            status, _, payload = self.request("GET", "/api/gem-hunter/discover?limit=20")
        result = json.loads(payload)
        self.assertEqual(status, 200)
        self.assertEqual(result["signal_status"], "BUY_FOUND")
        self.assertEqual(result["qualified_buy_count"], 1)
        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(result["discovered_candidate_count"], 2)
        self.assertEqual(result["evaluation_record_count"], 2)
        self.assertEqual(result["complete_evaluation_count"], 2)
        self.assertEqual(result["unassessed_candidate_count"], 0)
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["candidates"][0]["trade_authorization"], "BUY")
        self.assertNotIn("available_candidate_count", result)
        self.assertNotIn("trade_authorization", result)
        # The only returned token is the qualified BUY; the UNSCREENED twin is hidden.

    def test_gem_hunter_endpoint_hides_all_unqualified_candidates(self):
        web_app.GEM_HUNTER_CACHE.update(expires_at=0.0, payload=None)
        web_app.GEM_HUNTER_RESULT_CACHE.update(
            expires_at=0.0, payload=None, refreshing=False, last_error=None,
        )
        raw = {
            "status": "PASS", "candidate_count": 1,
            "candidates": [{
                "chain": "solana", "token_address": "61iusjsjCqfCW6SViHytSyYqvALUMcv3k2xMsUtM1UwP",
                "candidate_status": "UNSCREENED", "trade_authorization": "NO_TRADE",
            }],
            "trade_authorization": "NO_TRADE",
        }
        with patch.object(web_app, "discover_gem_candidates", return_value=raw), \
             patch.object(web_app, "reconcile_discovery_candidates", side_effect=lambda rows, http: rows), \
             patch.object(web_app, "screen_discovery_candidates", side_effect=lambda rows, data: rows), \
             patch.object(web_app, "_start_gem_hunter_refresh", side_effect=web_app._refresh_gem_hunter_result):
            status, _, payload = self.request("GET", "/api/gem-hunter/discover?limit=20")
        result = json.loads(payload)
        self.assertEqual(status, 200)
        self.assertEqual(result["signal_status"], "EVALUATION_INCOMPLETE")
        self.assertEqual(result["qualification_engine_status"], "INCOMPLETE")
        self.assertEqual(result["discovered_candidate_count"], 1)
        self.assertEqual(result["evaluation_record_count"], 1)
        self.assertEqual(result["complete_evaluation_count"], 0)
        self.assertEqual(result["incomplete_evaluation_count"], 1)
        self.assertEqual(result["unassessed_candidate_count"], 0)
        self.assertIn("شواهد", result["diagnostic_fa"])
        self.assertEqual(result["qualified_buy_count"], 0)
        self.assertEqual(result["candidates"], [])
        self.assertFalse(result["public_policy"]["discovery_candidates_exposed"])
        self.assertNotIn("available_candidate_count", result)
        self.assertNotIn("61iusjsjCqfCW6SViHytSyYqvALUMcv3k2xMsUtM1UwP", json.dumps(result))

    def test_gem_hunter_report_exposes_research_candidates_separately_from_public_buy_api(self):
        web_app.GEM_HUNTER_CACHE.update(expires_at=0.0, payload=None)
        web_app.GEM_HUNTER_RESULT_CACHE.update(
            expires_at=0.0, payload=None, refreshing=False, last_error=None,
        )
        address = "61iusjsjCqfCW6SViHytSyYqvALUMcv3k2xMsUtM1UwP"
        raw = {
            "status": "PASS", "candidate_count": 1,
            "candidates": [{
                "chain": "solana", "token_address": address,
                "token_name": "Research Token", "token_symbol": "RSH",
                "pool_address": "PoolAddressExample123",
                "liquidity_usd": 120000, "volume_24h_usd": 180000,
                "volume_to_liquidity_24h": 1.5,
                "candidate_status": "UNSCREENED", "trade_authorization": "NO_TRADE",
            }],
            "trade_authorization": "NO_TRADE",
        }
        with patch.object(web_app, "discover_gem_candidates", return_value=raw), \
             patch.object(web_app, "reconcile_discovery_candidates", side_effect=lambda rows, http: rows), \
             patch.object(web_app, "screen_discovery_candidates", side_effect=lambda rows, data: rows), \
             patch.object(web_app, "_start_gem_hunter_refresh", side_effect=web_app._refresh_gem_hunter_result):
            status, _, payload = self.request("GET", "/api/gem-hunter/report?limit=50")
        result = json.loads(payload)
        self.assertEqual(status, 200)
        self.assertEqual(result["report_mode"], "RESEARCH_ONLY_NOT_A_TRADE_SIGNAL")
        self.assertTrue(result["public_policy"]["discovery_candidates_exposed"])
        self.assertFalse(result["public_policy"]["diagnostic_candidates_are_trade_signals"])
        self.assertEqual(result["discovered_candidate_count"], 1)
        self.assertEqual(result["candidates"], [])
        self.assertEqual(len(result["diagnostic_candidates"]), 1)
        candidate = result["diagnostic_candidates"][0]
        self.assertEqual(candidate["token_name"], "Research Token")
        self.assertEqual(candidate["token_symbol"], "RSH")
        self.assertEqual(candidate["token_address"], address)
        self.assertEqual(candidate["trade_authorization"], "NO_TRADE")
        self.assertEqual(len(candidate["qualification"]["filters"]), 12)
        self.assertEqual(len(candidate["qualification"]["critical_gates"]), 4)

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
            scan_page = first_payload.decode("utf-8")
            self.assertIn("pollAttempts=0", scan_page)
            self.assertIn("setTimeout(poll,delay)", scan_page)
            self.assertIn("pollStarted", scan_page)
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
