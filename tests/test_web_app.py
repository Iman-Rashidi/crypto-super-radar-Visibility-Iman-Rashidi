"""Offline integration tests for the dependency-light HTTP dashboard."""
from __future__ import annotations

import json
from datetime import datetime, timezone
import os
import queue
import re
import shutil
import subprocess
import tempfile
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
        self.assertIn("crypto-super-radar.gem-hunter-open-reports.v1", page)
        self.assertIn("persistExpandedReportKeys", page)
        self.assertIn("watchlistIdentity", page)
        self.assertIn("✓ مورد با موفقیت در فهرست زیر نظر همین مرورگر ذخیره شد.", page)
        self.assertIn("افزودن ناموفق؛ جزئیات را بررسی کنید", page)
        self.assertIn("ربات معامله‌گر آزمایشی", page)
        self.assertIn('id="ai-diagnostic-panel"', page)
        self.assertIn("آزمایش واقعی اتصال Nemotron", page)
        self.assertIn('id="ai-dry-run-panel"', page)
        self.assertIn("اجرای تست واقعی Nemotron (Dry-Run)", page)
        self.assertIn("/api/ai/dry-run", page)
        self.assertIn("سقف زیان روزانه", page)
        self.assertIn("پوشش دادهٔ کارمزد", page)
        self.assertIn("اجزای کارمزد مشاهده‌شده", page)
        self.assertIn('action="/paper-bot/tick"', page)
        self.assertIn('id="futures-paper-bot"', page)
        self.assertIn('action="/futures-paper-bot/tick"', page)
        self.assertIn('value="perp:BTCUSDT"', page)
        self.assertIn("/futures-paper-bot/status", page)
        self.assertIn("USDT-M Perpetual", page)
        self.assertIn("python scripts/paper_trading_bot.py", page)
        self.assertIn("ماشین‌حساب حرفه‌ای سناریوی Spot", page)
        self.assertIn('id="calc-amount"', page)
        self.assertIn("calc-break-even", page)
        self.assertIn("بازار رمزارزها", page)
        self.assertIn('id="global-market-board"', page)
        self.assertIn("/api/global-markets", page)
        self.assertIn("TWELVEDATA_API_KEY", page)
        self.assertIn('value="cex:XRPUSDT"', page)
        self.assertIn('value="cex:AVAXUSDT"', page)
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
        self.assertIn('id="radar-gem-hunter"', page)
        self.assertIn('id="radar-market"', page)
        self.assertIn('id="radar-dex-search"', page)
        self.assertIn('class="radar-sweep"', page)
        self.assertIn('window.CryptoRadar = {update, scanning, error}', page)
        self.assertIn('BUY فقط پس از تأیید کامل', page)
        scan_result_page = web_app._page("<!--RADAR_ASSET_SCAN-->").decode("utf-8")
        self.assertIn('id="radar-asset-scan"', scan_result_page)
        self.assertIn("انتخاب برای اسکن", page)
        self.assertIn("scanButton.disabled=item.status!=='PASS'", page)
        self.assertIn("input.value='cex:'+item.symbol", page)
        self.assertIn('class="app-header"', page)
        self.assertIn("var(--teal)", page)
        self.assertLess(page.index('id="crypto-markets"'), page.index('id="scan-section"'))

    def test_futures_paper_status_endpoint_returns_paper_only_state(self):
        payload = {
            "mode": "FUTURES_PAPER_ONLY",
            "market": "USDT-M_PERPETUAL",
            "leverage": 1.0,
            "orders_submitted": False,
            "account": {"balance_usd": 10_000.0, "equity_usd": 10_000.0},
            "open_position": None,
            "risk_controls": {"entry_allowed": True},
        }
        with patch.object(web_app.FUTURES_PAPER_BOT, "state", return_value=payload) as state:
            status, _, body = self.request("GET", "/futures-paper-bot/status")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), payload)
        state.assert_called_once_with()
        self.assertFalse(json.loads(body)["orders_submitted"])

    def test_futures_cycle_report_translates_unknown_market_data_failure(self):
        report = web_app._futures_paper_cycle_report({
            "status": "UNKNOWN",
            "asset": "perp:ETHUSDT",
            "reason_code": "PERPETUAL_MARKET_DATA_UNAVAILABLE",
            "technical_error_type": "DataProviderError",
            "technical_detail": "HTTP 451 from fapi.binance.com",
        })
        self.assertEqual(report["status_fa"], "نتیجه نامشخص؛ چرخه به‌صورت ایمن متوقف شد")
        self.assertEqual(report["decision_fa"], "توقف ایمن؛ داده یا وضعیت لازم تأیید نشد")
        self.assertEqual(report["direction_fa"], "بدون معامله")
        self.assertTrue(report["entry_blocked"])
        self.assertFalse(report["orders_submitted"])
        self.assertIn("دادهٔ لحظه‌ای قرارداد دائمی", report["reason_fa"])

    def test_blofin_elliott_wave_report_selects_directional_scenario_and_discloses_alignment(self):
        wave = {
            "status": "CANDIDATE",
            "pattern": "BULLISH",
            "phase": "WAVES_1_AND_2_CONFIRMED",
            "wave_count": 2,
            "next_wave": 3,
            "invalidation_price": 64200.0,
            "pivot_reversal_threshold_pct": 3.25,
            "pivots": [{
                "kind": "LOW", "price": 64000.0,
                "timestamp": "2026-11-14T10:00:00+00:00",
                "confirmed_at": "2026-11-14T12:00:00+00:00",
            }],
        }
        long_report = web_app._blofin_elliott_wave_report_fa(wave, "LONG")
        short_report = web_app._blofin_elliott_wave_report_fa(wave, "SHORT")

        self.assertEqual(long_report["pattern_fa"], "صعودی")
        self.assertEqual(long_report["direction_alignment"], "ALIGNED")
        self.assertIn("موج ۳ صعودی", long_report["selected_wave_fa"])
        self.assertEqual(long_report["invalidation_price"], 64200.0)
        self.assertEqual(long_report["pivots"][0]["kind_fa"], "کف تأییدشده")
        self.assertEqual(short_report["direction_alignment"], "CONFLICT")
        self.assertIn("هم‌جهت نیست", short_report["direction_alignment_fa"])
        self.assertIn("احتمال موفقیت", long_report["confidence_note_fa"])

    def test_blofin_execution_report_is_persian_and_contains_live_timing_and_gate_details(self):
        wave = web_app._blofin_elliott_wave_report_fa({
            "status": "CANDIDATE", "pattern": "BEARISH",
            "phase": "WAVE_3_CONFIRMED", "wave_count": 3, "next_wave": 4,
            "invalidation_price": 67250.0, "pivots": [],
        }, "NO_TRADE")
        job = {
            "status": "completed",
            "stage": "NO_TRADE",
            "message_fa": "شرایط ورود تأیید نشد.",
            "updated_at_utc": "2026-11-14T15:36:46.503528+00:00",
            "analysis": {
                "signal": "NO_TRADE", "gate_pass_count": 0,
                "gate_fail_count": 0, "gate_unknown_count": 12,
                "gates_fa": [{
                    "gate_id": 1, "name_fa": "کیفیت داده بازار",
                    "status_fa": "نامشخص", "reason_fa": "دادهٔ معتبر دریافت نشد.",
                }],
            },
            "elliott_wave": wave,
            "trade_context": {
                "analysis_time_utc": "2026-11-14T15:36:43+00:00",
                "last_completed_candle_time_utc": "2026-11-14T00:00:00+00:00",
                "last_candle_close": 66100.0,
            },
            "events": [{
                "time_utc": "2026-11-14T15:36:43+00:00",
                "status": "running", "stage": "ELLIOTT_WAVE_ANALYSIS",
                "message_fa": "ساختار موج الیوت بررسی شد.",
            }],
            "result": {
                "status": "NO_TRADE",
                "reason_fa": "۱۲ فیلتر تأیید نشد؛ هیچ سفارشی ارسال نشد.",
                "orders_submitted": False,
                "trade_executed": False,
            },
        }

        report = web_app._blofin_execution_report_fa(job)

        self.assertEqual(report["status_fa"], "معامله‌ای انجام نشد")
        self.assertEqual(report["signal_fa"], "بدون معامله")
        self.assertIn("۰ فیلتر تأیید", report["gate_summary_fa"])
        self.assertIn("۱۲ فیلتر نامشخص", report["gate_summary_fa"])
        self.assertEqual(report["analysis_time_utc"], "2026-11-14T15:36:43+00:00")
        self.assertIn("موج ۴ نزولی", report["elliott_wave"]["selected_wave_fa"])
        self.assertEqual(report["events_fa"][0]["stage_fa"], "تحلیل و انتخاب سناریوی موج الیوت")
        self.assertFalse(report["orders_submitted"])
        self.assertFalse(report["trade_executed"])

    def test_blofin_dashboard_inline_javascript_passes_node_syntax_check(self):
        if shutil.which("node") is None:
            self.skipTest("Node.js is not available in this test environment")
        status, _, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        page = body.decode("utf-8")
        panel_start = page.index('id="blofin-demo-panel"')
        script_tag = page.index("<script>", panel_start)
        script_start = page.index("\\n", script_tag) + 1
        script_end = page.index("</script>", script_start)
        script = page[script_start:script_end]
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".js", delete=False
        ) as handle:
            handle.write(script)
            script_path = handle.name
        try:
            result = subprocess.run(
                ["node", "--check", script_path],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
        finally:
            os.unlink(script_path)

    def test_blofin_execution_dashboard_renders_persian_report_instead_of_raw_json(self):
        status, _, body = self.request("GET", "/")
        page = body.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn('id="blofin-execution-report"', page)
        self.assertIn("function renderExecutionJob(job){", page)
        self.assertNotIn("function renderExecutionJob(job){{", page)
        self.assertNotIn("const report=job&&job.report_fa?job.report_fa:{{}};", page)
        self.assertIn("checkButton.click();", page)
        self.assertIn("انتخاب ساختار موج الیوت در لحظه تحلیل", page)
        self.assertIn("خط زمانی رویدادهای چرخه", page)
        self.assertNotIn("JSON.stringify(job,null,2)", page)
        self.assertNotIn('id="blofin-execution-result"', page)

    def test_dashboard_does_not_label_risk_circuit_breaker_as_trade_permission(self):
        status, _, body = self.request("GET", "/")
        page = body.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn("به‌معنای مجوز نهایی ورود نیست", page)
        self.assertNotIn("risk.entry_allowed?'ورود مجاز'", page)

    def test_ai_status_endpoint_reports_configuration_without_exposing_secret(self):
        payload = {
            "provider": "OpenRouter",
            "model": "nvidia/nemotron-3-ultra-550b-a55b:free",
            "status": "CONFIGURED",
            "configured": True,
            "secret_sent_to_model": False,
        }
        with patch.object(web_app, "ai_connection_status", return_value=payload):
            status, _, body = self.request("GET", "/api/ai/status")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), payload)
        self.assertNotIn("api_key", body.decode().lower())

    def test_ai_diagnostic_endpoint_is_same_origin_and_does_not_start_paper_bot(self):
        payload = {
            "provider": "OpenRouter",
            "model": "nvidia/nemotron-3-ultra-550b-a55b:free",
            "status": "CONNECTED",
            "probe": "PASS",
            "model_verified": True,
            "paper_entry_eligible": False,
            "orders_submitted": False,
        }
        with patch.object(web_app, "_run_ai_diagnostic_limited", return_value=payload) as probe:
            status, _, body = self.request(
                "POST", "/api/ai/diagnostic", "confirm=1",
                {
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": f"http://{self.host}:{self.port}",
                },
            )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), payload)
        probe.assert_called_once_with()
        self.assertFalse(json.loads(body)["paper_entry_eligible"])
        self.assertFalse(json.loads(body)["orders_submitted"])

    def test_ai_synthetic_dry_run_calls_only_the_ai_review_and_forces_trade_flags_off(self):
        payload = {
            "provider": "OpenRouter",
            "model": "nvidia/nemotron-3-ultra-550b-a55b:free",
            "status": "APPROVED",
            "model_verified": True,
            "response_validated": True,
            "test_passed": True,
            "paper_entry_eligible": False,
            "orders_submitted": False,
            "trade_executed": False,
            "test_mode": "SYNTHETIC_DRY_RUN",
        }
        with patch.object(web_app, "_run_ai_synthetic_dry_run_limited", return_value=payload) as dry_run, \
             patch.object(web_app.FUTURES_PAPER_BOT, "run_once") as futures_tick, \
             patch.object(web_app.PAPER_BOT, "run_once") as spot_tick:
            status, _, body = self.request(
                "POST", "/api/ai/dry-run", "confirm=1",
                {
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": f"http://{self.host}:{self.port}",
                },
            )
        result = json.loads(body)
        self.assertEqual(status, 200)
        dry_run.assert_called_once_with()
        futures_tick.assert_not_called()
        spot_tick.assert_not_called()
        self.assertFalse(result["paper_entry_eligible"])
        self.assertFalse(result["orders_submitted"])
        self.assertFalse(result["trade_executed"])

    def test_ai_synthetic_dry_run_rejects_cross_origin_requests(self):
        with patch.object(web_app, "_run_ai_synthetic_dry_run_limited") as dry_run:
            status, _, body = self.request(
                "POST", "/api/ai/dry-run", "confirm=1",
                {
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": "https://attacker.invalid",
                },
            )
        self.assertEqual(status, 403)
        self.assertIn(b"Cross-origin", body)
        dry_run.assert_not_called()

    def test_ai_diagnostic_endpoint_rejects_cross_origin_requests(self):
        with patch.object(web_app, "_run_ai_diagnostic_limited") as probe:
            status, _, body = self.request(
                "POST", "/api/ai/diagnostic", "confirm=1",
                {
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": "https://attacker.invalid",
                },
            )
        self.assertEqual(status, 403)
        self.assertIn(b"Cross-origin scan requests are not allowed", body)
        probe.assert_not_called()

    def test_futures_paper_post_rejects_spot_asset(self):
        body = "asset=cex%3ABTCUSDT"
        status, _, payload = self.request(
            "POST", "/futures-paper-bot/tick", body,
            {"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(status, 400)
        self.assertIn(b"Futures paper trading accepts only", payload)

    def test_global_market_endpoint_is_read_only_and_passes_refresh_flag(self):
        payload = {
            "status": "OBSERVED_SINGLE_SOURCE_NOT_VALIDATED",
            "provider": "Twelve Data",
            "configured": True,
            "validation_status": "SINGLE_SOURCE_ONLY",
            "trade_eligible": False,
            "orders_submitted": False,
            "items": [],
            "counts": {"total": 24, "observed": 0, "unknown": 24},
        }
        with patch.object(web_app.GLOBAL_MARKET_BOARD, "get_board", return_value=payload) as board:
            status, _, body = self.request("GET", "/api/global-markets?refresh=1")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), payload)
        board.assert_called_once_with(force_refresh=True)
        self.assertFalse(json.loads(body)["trade_eligible"])
        self.assertFalse(json.loads(body)["orders_submitted"])

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
        self.assertEqual(result["performance"]["fee_data_status"], "NO_DATA")
        self.assertIsNone(result["performance"]["fee_coverage_pct"])

    def test_paper_bot_cycle_report_translates_no_trade_and_performance_into_persian(self):
        cycle = {
            "status": "NO_TRADE",
            "asset": "cex:SOLUSDT",
            "reason": "Every mandatory signal gate must PASS before paper entry.",
        }
        paper_state = {
            "account": {
                "initial_equity_usd": 10000.0,
                "cash_balance_usd": 10000.0,
                "equity_usd": 10000.0,
                "realized_pnl_usd": 0.0,
                "unrealized_pnl_usd": 0.0,
                "total_return_pct": 0.0,
            },
            "open_position": None,
            "recent_trades": [],
            "performance": {
                "status": "INSUFFICIENT_SAMPLE",
                "sample_size": 0,
                "minimum_sample_size": 30,
                "performance_claim_allowed": False,
                "fee_data_status": "NO_DATA",
                "slippage_data_status": "NO_DATA",
                "spread_data_status": "NO_DATA",
                "funding_data_status": "NO_DATA",
                "execution_cost_attribution_status": "NO_DATA",
                "reason": "No valid closed paper trades are recorded yet.",
            },
        }
        safety = {
            "read_only": True,
            "paper_only": True,
            "wallet_credentials_used": False,
            "private_exchange_credentials_used": False,
            "orders_submitted": False,
            "transactions_signed": False,
        }
        report = web_app._paper_tick_report_fa(cycle, paper_state, safety)
        self.assertEqual(report["cycle"][0]["value"], "معامله‌ای انجام نشد")
        self.assertEqual(report["cycle"][1]["value"], "SOLUSDT")
        self.assertIn("تمام دروازه‌های اجباری سیگنال", report["cycle"][2]["value"])
        self.assertEqual(report["performance"][0]["value"], "تعداد نمونهٔ آماری کافی نیست")
        self.assertEqual(report["performance"][1]["value"], 0)
        self.assertEqual(report["performance"][2]["value"], 30)
        self.assertEqual(report["performance"][3]["value"], "مجاز نیست")
        self.assertIn("معاملهٔ آزمایشیِ بسته‌شده", report["performance"][-1]["value"])
        self.assertIn("هیچ موقعیت بازی", report["open_position"]["note"])
        self.assertIn("هیچ سفارش واقعی", " ".join(report["execution_cost_notes"]))
        self.assertNotIn("Every mandatory signal gate", json.dumps(report, ensure_ascii=False))

    def test_paper_tick_worker_attaches_persian_report_without_changing_no_trade(self):
        job_id = "paper-cycle-persian-test"
        old_job = web_app.JOBS.get(job_id)
        cycle = {
            "status": "NO_TRADE",
            "asset": "cex:SOLUSDT",
            "reason": "Every mandatory signal gate must PASS before paper entry.",
        }
        paper_state = {
            "mode": "PAPER_ONLY",
            "account": {"initial_equity_usd": 10000.0, "cash_balance_usd": 10000.0,
                        "equity_usd": 10000.0, "realized_pnl_usd": 0.0,
                        "unrealized_pnl_usd": 0.0, "total_return_pct": 0.0},
            "open_position": None,
            "recent_trades": [],
            "performance": {"status": "INSUFFICIENT_SAMPLE", "sample_size": 0,
                            "minimum_sample_size": 30, "performance_claim_allowed": False,
                            "reason": "No valid closed paper trades are recorded yet."},
        }
        try:
            with patch.object(web_app.PAPER_BOT, "run_once", return_value=cycle), \
                 patch.object(web_app.PAPER_BOT, "state", return_value=paper_state):
                web_app._run_paper_tick(job_id, "cex:SOLUSDT")
            job = web_app.JOBS[job_id]
            self.assertEqual(job["status"], "completed")
            self.assertEqual(job["result"]["cycle"]["status"], "NO_TRADE")
            self.assertIn("reason_fa", job["result"]["cycle"])
            self.assertEqual(job["result"]["report_fa"]["cycle"][0]["value"], "معامله‌ای انجام نشد")
            self.assertFalse(job["result"]["safety"]["orders_submitted"])
        finally:
            web_app.JOBS.pop(job_id, None)
            if old_job is not None:
                web_app.JOBS[job_id] = old_job

    def test_paper_tick_result_page_renders_persian_report_not_raw_json(self):
        script = web_app._paper_tick_page_script("test-cycle-id")
        self.assertIn("renderPaperBotReport", script)
        self.assertIn("گزارش فارسی از سرور دریافت نشد", script)
        self.assertIn("همان چرخه به‌طور خودکار دوباره بررسی می‌شود", script)
        self.assertNotIn("JSON.stringify", script)

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
            self.assertIn("CryptoRadar.scanning('radar-asset-scan'", scan_page)
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
