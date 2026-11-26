"""Offline integration tests for the dependency-light HTTP dashboard."""
from __future__ import annotations

import atexit
import json
import inspect
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
from unittest.mock import ANY, Mock, patch
from http.server import ThreadingHTTPServer
from http.client import HTTPConnection
from urllib.parse import quote

from crypto_super_radar.web_app import Handler
from crypto_super_radar import web_app


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

    def test_blofin_demo_challenge_panel_and_readiness_endpoint_are_read_only(self):
        status, _, payload = self.request("GET", "/api/blofin/demo-challenges")
        self.assertEqual(status, 200)
        data = json.loads(payload)
        self.assertEqual(data["mode"], "READ_ONLY_PUBLIC_DATA")
        self.assertFalse(data["orders_submitted"])
        self.assertFalse(data["registration_submitted"])
        page_status, _, page = self.request("GET", "/")
        self.assertEqual(page_status, 200)
        rendered = page.decode("utf-8")
        self.assertIn("شکارچی مسابقات رایگان BloFin Demo", rendered)
        self.assertIn("/api/blofin/demo-challenges/scan", rendered)

    def test_blofin_demo_challenge_scan_requires_same_origin(self):
        status, _, payload = self.request(
            "POST", "/api/blofin/demo-challenges/scan", "{}",
            {"Content-Type":"application/json"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(payload)["code"], "SAME_ORIGIN_REQUIRED")

    def test_blofin_demo_challenge_scan_is_read_only_and_returns_scanner_report(self):
        report = {
            "schema_version":"1.0", "mode":"READ_ONLY_PUBLIC_DATA",
            "orders_submitted":False, "registration_submitted":False,
            "results":[], "errors":[], "scanned_at_utc":"2026-11-26T00:00:00+00:00",
        }
        with patch.object(web_app, "scan_blofin_demo_challenges", return_value=report) as scanner:
            status, _, payload = self.request(
                "POST", "/api/blofin/demo-challenges/scan", "{}",
                {"Content-Type":"application/json", "Origin":f"http://{self.host}:{self.port}"},
            )
        self.assertEqual(status, 200)
        data = json.loads(payload)
        self.assertEqual(data["mode"], "READ_ONLY_PUBLIC_DATA")
        self.assertFalse(data["orders_submitted"])
        self.assertFalse(data["registration_submitted"])
        scanner.assert_called_once()

    def test_disabled_autopilot_startup_clears_stale_next_cycle_metadata(self):
        previous = dict(web_app.BLOFIN_AUTOPILOT_STATE)
        try:
            web_app.BLOFIN_AUTOPILOT_STATE.update({
                "enabled": True,
                "status": "WAITING_FOR_NEXT_5M_CANDLE",
                "search_continues": True,
                "next_cycle_scheduled": True,
                "next_cycle_at_utc": "2026-11-18T12:25:03+00:00",
                "next_cycle_in_seconds": 120,
            })
            with patch.dict(os.environ, {
                "BLOFIN_DEMO_AUTOPILOT_ROLE": "WEB",
                "BLOFIN_DEMO_AUTOPILOT_ENABLED": "false",
            }), patch.object(
                web_app, "_restore_blofin_autopilot_state",
            ), patch.object(
                web_app, "_schedule_blofin_demo_startup_recovery",
            ), patch.object(
                web_app, "_persist_blofin_autopilot_state",
            ):
                web_app._start_blofin_demo_autopilot()

            state = web_app.BLOFIN_AUTOPILOT_STATE
            self.assertFalse(state["enabled"])
            self.assertEqual(state["status"], "DISABLED")
            self.assertFalse(state["search_continues"])
            self.assertFalse(state["next_cycle_scheduled"])
            self.assertIsNone(state["next_cycle_at_utc"])
            self.assertIsNone(state["next_cycle_in_seconds"])
        finally:
            web_app.BLOFIN_AUTOPILOT_STATE.clear()
            web_app.BLOFIN_AUTOPILOT_STATE.update(previous)

    def test_notion_sync_worker_error_is_visible_in_status_api(self):
        previous = dict(web_app.NOTION_EVENT_SYNC_STATE)
        try:
            web_app._record_notion_event_sync_state(error_type="OperationalError")

            class FakeOutbox:
                def status_snapshot(self):
                    return {
                        "status": "DEGRADED",
                        "pending": 1,
                        "retrying": 1,
                        "last_error_type": "HTTPError",
                        "last_http_status": 503,
                        "recent_errors": [
                            {"error_type": "HTTPError", "http_status": 503}
                        ],
                    }

                def close(self):
                    pass

            class FakeActivity:
                def status_snapshot(self):
                    return {"status": "CONFIGURED", "pending": 0, "synced": 1}

                def close(self):
                    pass

            with patch.object(web_app, "NotionEventOutbox", FakeOutbox), patch.object(
                web_app, "NotionActivityJournal", FakeActivity,
            ):
                status, _headers, payload = self.request(
                    "GET", "/api/notion-sync/status"
                )

            data = json.loads(payload)
            self.assertEqual(status, 200)
            self.assertEqual(data["status"], "DEGRADED")
            self.assertEqual(data["last_error_type"], "HTTPError")
            self.assertEqual(data["last_http_status"], 503)
            self.assertEqual(data["worker"]["status"], "WORKER_ERROR")
            self.assertEqual(
                data["worker"]["last_worker_error_type"], "OperationalError"
            )
            self.assertEqual(data["activity_journal"]["status"], "CONFIGURED")
        finally:
            web_app.NOTION_EVENT_SYNC_STATE.clear()
            web_app.NOTION_EVENT_SYNC_STATE.update(previous)

    def test_autopilot_status_hides_stale_schedule_when_disabled(self):
        previous = dict(web_app.BLOFIN_AUTOPILOT_STATE)
        try:
            with web_app.LOCK:
                web_app.BLOFIN_AUTOPILOT_STATE.update({
                    "enabled": True,
                    "status": "WAITING_FOR_NEXT_5M_CANDLE",
                    "search_continues": True,
                    "next_cycle_scheduled": True,
                    "next_cycle_at_utc": "2026-11-18T12:30:00+00:00",
                    "next_cycle_in_seconds": 120,
                })
            with (
                patch.dict(os.environ, {"BLOFIN_DEMO_AUTOPILOT_ENABLED": "false"}),
                patch.object(
                    web_app, "_blofin_demo_execution_readiness",
                    return_value={"status": "BLOCKED", "trading_enabled": False},
                ),
            ):
                status, _headers, payload = self.request(
                    "GET", "/api/blofin/demo/autopilot/status?status_only=1"
                )

            data = json.loads(payload)
            self.assertEqual(status, 200)
            self.assertFalse(data["enabled"])
            self.assertEqual(data["status"], "DISABLED")
            self.assertFalse(data["search_continues"])
            self.assertFalse(data["next_cycle_scheduled"])
            self.assertIsNone(data["next_cycle_at_utc"])
            self.assertIsNone(data["next_cycle_in_seconds"])
            self.assertEqual(
                data.get("last_cycle_diagnostics"),
                previous.get("last_cycle_diagnostics"),
            )
        finally:
            with web_app.LOCK:
                web_app.BLOFIN_AUTOPILOT_STATE.clear()
                web_app.BLOFIN_AUTOPILOT_STATE.update(previous)

    def test_embedded_http_retry_regex_uses_python_safe_escaping(self):
        with open(web_app.__file__, encoding="utf-8") as source_file:
            source = source_file.read()
        self.assertIn(r"/^HTTP (429|5\\d\\d)$/.test(message);", source)

    def test_send_silently_handles_disconnected_client(self):
        class BrokenPipeStream:
            def write(self, _content):
                raise BrokenPipeError("client disconnected")

        handler = object.__new__(Handler)
        handler.close_connection = False
        handler.send_response = lambda _status: None
        handler.send_header = lambda _name, _value: None
        handler.end_headers = lambda: None
        handler.wfile = BrokenPipeStream()

        self.assertFalse(handler._send(200, b"response"))
        self.assertTrue(handler.close_connection)

    def test_home_page_is_persian_and_separates_gem_hunter_v3_from_twelve_gates(self):
        status, headers, payload = self.request("GET", "/")
        page = payload.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn('lang="fa" dir="rtl"', page)
        self.assertIn("۱۲ فیلتر اجباری", page)
        self.assertIn("Gem Hunter V3 یک موتور مستقل و سبک", page)
        self.assertIn("اجرای اسکن زنده Gem Hunter V3", page)
        self.assertIn("Solana، Base و Ethereum", page)
        self.assertIn("Tier 2", page)
        self.assertNotIn("حداقل ۹ فیلتر از ۱۲", page)
        self.assertIn("کشف‌شده:", page)
        self.assertIn("وضعیت منابع داده", page)
        self.assertIn("Paper Research", page)
        self.assertIn('id="gem-hunter-watchlist"', page)
        self.assertNotIn('id="watchlist-manual-form"', page)
        self.assertIn('id="gem-watchlist-rows"', page)
        self.assertIn('/api/gem-hunter/v3/watchlist', page)
        self.assertIn('load();', page)
        self.assertIn('id="exchange-shortcuts"', page)
        self.assertIn('cdn.simpleicons.org/binance', page)
        self.assertIn('cdn.simpleicons.org/bybit', page)
        self.assertIn('cdn.simpleicons.org/okx', page)
        self.assertIn('/api/gem-hunter/v3/latest', page)
        self.assertNotIn('/api/gem-hunter/v2/candidates?limit=10', page)
        self.assertIn('Tier 1', page)
        self.assertIn('Tier 2', page)
        self.assertIn("crypto-super-radar.gem-hunter-open-reports.v1", page)
        self.assertIn("persistExpandedReportKeys", page)
        self.assertIn("ربات معامله‌گر آزمایشی", page)
        self.assertIn('id="ai-diagnostic-panel"', page)
        self.assertIn("آزمایش واقعی اتصال Nemotron", page)
        self.assertIn('id="ai-dry-run-panel"', page)
        self.assertIn("اجرای تست واقعی Groq (Dry-Run)", page)
        self.assertIn("https://blofin.com/trade/en/futures/BTC-USDT", page)
        self.assertIn("https://blofin.com/market/derivatives", page)
        self.assertIn("https://blofin.com/en/support/FAQ/Futures-Trading/11742286589455-What-is-Demo-Trading", page)
        self.assertIn("راهنمای رسمی ورود به BloFin Demo", page)
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
            "timeframe": "5m",
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
        self.assertEqual(long_report["timeframe_fa"], "۵ دقیقه‌ای")
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

    def test_autopilot_ai_summary_exposes_actual_reviewer_metadata_only(self):
        job = {
            "ai_reviews": [
                {
                    "reviewer_id": "nemotron",
                    "review": {
                        "provider": "OpenRouter",
                        "model": "configured-model",
                        "returned_model": "actual-model",
                        "status": "APPROVED",
                        "request_attempted": True,
                        "response_source": "LIVE_API",
                        "response_validated": True,
                        "model_verified": True,
                        "request_started_at_utc": "2026-11-18T12:30:00+00:00",
                        "completed_at_utc": "2026-11-18T12:30:02+00:00",
                        "http_status": 401,
                        "latency_ms": 492,
                        "error_type": "HTTPError",
                        "error_code": "PROVIDER_UNAUTHORIZED",
                        "raw_response": "must not be projected",
                        "api_key": "must never be projected",
                    },
                },
            ],
        }

        summary = web_app._blofin_ai_review_summary(job)

        self.assertEqual(len(summary), 1)
        self.assertEqual(summary[0]["reviewer_id"], "nemotron")
        self.assertEqual(summary[0]["provider"], "OpenRouter")
        self.assertEqual(summary[0]["model"], "configured-model")
        self.assertEqual(summary[0]["returned_model"], "actual-model")
        self.assertEqual(summary[0]["response_source"], "LIVE_API")
        self.assertTrue(summary[0]["response_validated"])
        self.assertTrue(summary[0]["model_verified"])
        self.assertEqual(summary[0]["http_status"], 401)
        self.assertEqual(summary[0]["error_type"], "HTTPError")
        self.assertEqual(summary[0]["error_code"], "PROVIDER_UNAUTHORIZED")
        self.assertEqual(summary[0]["latency_ms"], 492)
        self.assertNotIn("raw_response", summary[0])
        self.assertNotIn("api_key", summary[0])

    def test_challenge1_supervisors_run_before_order_readiness_blocks_execution(self):
        with open(web_app.__file__, encoding="utf-8") as source_file:
            source = source_file.read()
        start = source.index("def _run_blofin_demo_execution_job(")
        end = source.index("\ndef _blofin_demo_autopilot_worker(", start)
        execution_job = source[start:end]

        reviewer_call = execution_job.index(
            "_run_challenge1_supervisor_reviews("
        )
        self.assertIn('signal_id=str(record.get("signal_id") or "")', execution_job)
        order_readiness_check = execution_job.index(
            "_blofin_demo_execution_readiness()",
            reviewer_call,
        )
        self.assertLess(reviewer_call, order_readiness_check)

        blocked_branch = execution_job[order_readiness_check:]
        blocked_branch = blocked_branch[:blocked_branch.index(
            "client = BloFinDemoTradeClient(timeout=10.0)"
        )]
        self.assertIn('stage="TRADE_READINESS_BLOCKED"', blocked_branch)
        self.assertIn("ai_review=ai_review", blocked_branch)
        self.assertIn("ai_reviews=ai_reviews", blocked_branch)
        self.assertIn("ai_consensus=ai_consensus", blocked_branch)
        self.assertIn('"orders_submitted": False', blocked_branch)
        self.assertIn('"trade_executed": False', blocked_branch)
        self.assertIn("_queue_blofin_completed_job_reports(job_id, instrument_id)", blocked_branch)

    def test_blofin_dashboard_inline_javascript_passes_node_syntax_check(self):
        if shutil.which("node") is None:
            self.skipTest("Node.js is not available in this test environment")
        status, _, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        page = body.decode("utf-8")
        panel_start = page.index('id="blofin-demo-panel"')
        script_tag = page.index("<script>", panel_start)
        script_start = page.index("\n", script_tag) + 1
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
        self.assertIn("Mark Price واقعی همان قرارداد", page)
        self.assertIn("حد سود و حد ضرر فعال برای", page)
        self.assertIn("سفارش‌های تکمیل‌شده و تاریخچه سفارش", page)
        self.assertIn("رویدادهای ثبت‌شدهٔ ربات برای", page)
        self.assertIn("سفارش Live ندارد", page)
        self.assertIn("Spot Research: فقط خواندنی · BloFin: Demo", page)
        self.assertIn("۱۲ فیلتر عمومی فقط نظارتی", page)
        self.assertIn("استراتژی اصلی Challenge 1", page)
        self.assertIn("۱۲ فیلتر عمومی فقط نظارتی‌اند و شرط ورود نیستند", page)
        self.assertNotIn("قیمت، ۱۲ گیت، Nemotron و کنترل‌های ریسک در هر چرخه دوباره بررسی می‌شوند", page)
        self.assertIn("گزارش زنده تحلیل Spot · الگوهای کندلی و نموداری", page)
        self.assertIn("displayMonitorValue", page)
        self.assertNotIn("JSON.stringify(job,null,2)", page)
        self.assertNotIn('id="blofin-execution-result"', page)

    def test_manual_dashboard_is_fixed_to_challenge1_v2_single_shot(self):
        status, _, body = self.request("GET", "/")
        page = body.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn("استراتژی اجرای دستی CHALLENGE_1_V2", page)
        self.assertIn("کندل‌های بسته‌شدهٔ ۱۵ دقیقه، ۱ ساعت و ۴ ساعت", page)
        self.assertIn("دو تأیید مستقل و معتبر AI", page)
        self.assertIn("اجرای دستی یک چرخه نسخه جدید (CHALLENGE_1_V2 Single-Shot)", page)
        self.assertIn("const selectedProfile='CHALLENGE_1_V2';", page)
        self.assertIn("حالت اتوماتیک · Challenge 1", page)

    def test_trade_permission_block_does_not_block_analysis_when_durable_storage_is_ready(self):
        readiness = {
            "status": "BLOCKED",
            "trade_permission_status": "READ_ONLY",
            "trade_permission_verified": False,
            "trading_enabled": False,
            "durable_storage": True,
            "event_store_readable": True,
        }
        self.assertTrue(web_app._blofin_demo_analysis_storage_ready(readiness))
        self.assertFalse(web_app._blofin_demo_analysis_storage_ready({
            **readiness, "durable_storage": False,
        }))
        self.assertFalse(web_app._blofin_demo_analysis_storage_ready({
            **readiness, "event_store_readable": False,
        }))

    def test_execution_readiness_exposes_event_store_readability(self):
        class ReadableStore:
            def trade_budget_status(self):
                return {"orders_today": 0, "orders_total": 0, "unresolved_orders": 0}

            def single_shot_lock_status(self):
                return {
                    "enabled": True, "locked": False,
                    "max_lifetime_orders": 1, "consumed_orders": 0,
                    "client_order_id": None,
                }

            def close(self):
                pass

        empty_credentials = {
            "BLOFIN_DEMO_TRADE_API_KEY": "",
            "BLOFIN_DEMO_TRADE_API_SECRET": "",
            "BLOFIN_DEMO_TRADE_PASSPHRASE": "",
            "BLOFIN_DEMO_API_KEY": "",
            "BLOFIN_DEMO_API_SECRET": "",
            "BLOFIN_DEMO_PASSPHRASE": "",
            "BLOFIN_DEMO_TRADING_ENABLED": "false",
            "BLOFIN_DEMO_AUTOPILOT_ENABLED": "false",
            "BLOFIN_DASHBOARD_TOKEN": "",
        }
        with patch.dict(os.environ, empty_credentials, clear=False):
            with patch.object(web_app, "storage_backend", return_value="LOCAL_SQLITE"):
                with patch.object(web_app, "BloFinDemoEventStore", return_value=ReadableStore()):
                    result = web_app._blofin_demo_execution_readiness()

        self.assertFalse(result["durable_storage"])
        self.assertTrue(result["event_store_readable"])
        self.assertEqual(result["single_shot_lock"]["consumed_orders"], 0)
        self.assertFalse(result["single_shot_lock"]["locked"])
        self.assertEqual(result["status"], "BLOCKED")

    def test_dashboard_does_not_label_risk_circuit_breaker_as_trade_permission(self):
        status, _, body = self.request("GET", "/")
        page = body.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn("به‌معنای مجوز نهایی ورود نیست", page)
        self.assertNotIn("risk.entry_allowed?'ورود مجاز'", page)

    def test_ai_status_endpoint_reports_configuration_without_exposing_secret(self):
        payload = {
            "provider": "Independent Multi-Provider AI Supervisor",
            "model": "NVIDIA Nemotron + Gemini 3.8 Flash + Groq GPT-OSS 120B",
            "status": "READY",
            "configured": True,
            "configured_provider_count": 2,
            "required_independent_approvals": 2,
            "secret_sent_to_model": False,
        }
        with patch.object(web_app, "ai_supervisor_status", return_value=payload):
            status, _, body = self.request("GET", "/api/ai/status")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), payload)
        self.assertNotIn("api_key", body.decode().lower())

    def test_copilot_public_snapshot_preserves_groq_reviewer_vote_and_entry_levels(self):
        payload = {
            "signal": "LONG",
            "entry_price": "100.25",
            "stop_loss": "97.50",
            "take_profit": "106.00",
            "entry_skipped_reason": "GROQ_APPROVAL_NOT_PRESENT",
            "ai_reviews": [
                {
                    "reviewer_id": "groq",
                    "provider": "Groq",
                    "model": "openai/gpt-oss-20b",
                    "review": {
                        "status": "REJECTED",
                        "decision": "VETO",
                        "reason_fa": "ریسک نوسان بیش از حد است",
                        "model_verified": True,
                        "response_validated": True,
                        "request_attempted": True,
                        "http_status": 200,
                        "returned_model": "openai/gpt-oss-20b",
                    },
                },
            ],
        }
        row = {
            "cycle_id": "cycle-test",
            "symbol": "BTC-USDT",
            "started_at_utc": "2026-11-24T18:00:00Z",
            "finished_at_utc": "2026-11-24T18:00:05Z",
            "mode": "DRY_RUN",
            "status": "NO_TRADE",
            "signal": None,
            "reason": "دو رأی همسو نیستند",
            "orders_submitted": 0,
            "payload_json": json.dumps(payload),
        }
        connection = Mock()
        connection.execute.return_value = Mock(fetchall=Mock(return_value=[row]))
        with web_app.V2_PUBLIC_SNAPSHOT_CACHE_LOCK:
            web_app.V2_PUBLIC_SNAPSHOT_CACHE.update({"expires_at": 0.0, "payload": None})
        with (
            patch.object(web_app, "storage_backend", return_value="TURSO_CLOUD_SQLITE"),
            patch.object(web_app, "V2_PUBLIC_SNAPSHOT_CACHE_TTL_SECONDS", 30),
            patch.object(web_app, "connect_database", return_value=connection) as connect,
        ):
            snapshot = web_app._blofin_v2_public_cycle_snapshot()
            cached_snapshot = web_app._blofin_v2_public_cycle_snapshot()
        self.assertGreaterEqual(connect.call_count, 1)
        self.assertEqual(cached_snapshot, snapshot)
        self.assertEqual(snapshot["status"], "OK", snapshot)
        self.assertEqual(len(snapshot["cycles"]), 1)
        cycle = snapshot["cycles"][0]
        self.assertEqual(cycle["signal_raw"], "LONG")
        self.assertEqual(cycle["entry_price"], "100.25")
        self.assertEqual(cycle["stop_loss"], "97.50")
        self.assertEqual(cycle["take_profit"], "106.00")
        self.assertEqual(cycle["entry_skipped_reason"], "GROQ_APPROVAL_NOT_PRESENT")
        self.assertEqual(len(cycle["ai_votes"]), 1)
        self.assertEqual(cycle["ai_votes"][0]["reviewer_id"], "groq")
        self.assertEqual(cycle["ai_votes"][0]["provider"], "Groq")
        self.assertEqual(cycle["ai_votes"][0]["opinion"], "VETO")
        self.assertEqual(cycle["ai_votes"][0]["http_status"], 200)
        self.assertIn("نوسان", cycle["ai_votes"][0]["reason_fa"])
        self.assertFalse(cycle["orders_submitted"])

    def test_ai_copilot_context_includes_gem_identity_contract_price_liquidity_and_security(self):
        symbols = ["BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT", "LINK-USDT"]
        cycles = [{"symbol": symbol, "signal_raw": "NO_TRADE"} for symbol in symbols]
        gem_report = {
            "status": "OK",
            "generated_at_utc": "2026-11-24T19:00:00Z",
            "discovered_candidate_count": 1,
            "gem_hunter_v3_tier1_count": 1,
            "gem_hunter_v3_tier2_count": 0,
            "candidates": [{
                "token_name": "Example Token",
                "token_symbol": "EXT",
                "chain": "base",
                "token_address": "0x1234567890abcdef1234567890abcdef12345678",
                "price_usd": 0.0125,
                "liquidity_usd": 76543.21,
                "security_evidence": {
                    "status": "PASS",
                    "provider": "GOPLUS_SOURCIFY",
                    "reason_fa": ["بررسی حداقلی قرارداد عبور کرد."],
                    "flags": {"is_honeypot": False},
                },
            }],
        }
        with patch.object(web_app, "_blofin_v2_public_cycle_snapshot", return_value={
            "status": "OK", "environment": "BLOFIN_DEMO",
            "latest_cycle_id": "cycle-1", "scan_time_utc": "2026-11-24T19:00:00Z",
            "run_state": {"status": "IDLE"}, "cycles": cycles,
        }), patch.object(web_app, "latest_report_from_turso", return_value=gem_report), \
             patch.object(web_app, "_crypto_market_board", return_value=[]), \
             patch.object(web_app, "build_feed_health_report", return_value={"status": "OK"}):
            context = web_app._ai_copilot_dashboard_context()
        self.assertEqual(context["challenge1_v2"]["expected_markets"], symbols)
        self.assertEqual(context["challenge1_v2"]["missing_markets"], [])
        candidate = context["gem_hunter"]["top_candidates"][0]
        self.assertEqual(candidate["name"], "Example Token")
        self.assertEqual(candidate["symbol"], "EXT")
        self.assertEqual(candidate["chain"], "base")
        self.assertEqual(candidate["contract_address_short"], "0x1234…5678")
        self.assertEqual(candidate["price_usd"], 0.0125)
        self.assertEqual(candidate["liquidity_usd"], 76543.21)
        self.assertEqual(candidate["security_status"], "PASS")
        self.assertEqual(candidate["security_provider"], "GOPLUS_SOURCIFY")
        self.assertEqual(candidate["security_reason_fa"], ["بررسی حداقلی قرارداد عبور کرد."])
        self.assertFalse(context["orders_submitted"])

    def test_ai_copilot_chat_is_same_origin_and_read_only(self):
        payload = {"status": "OK", "answer": "فقط‌خواندنی", "read_only": True, "orders_submitted": False}
        with patch.object(web_app, "ai_copilot_chat", return_value=payload) as chat:
            status, _, body = self.request(
                "POST", "/api/ai-copilot/chat", json.dumps({"message": "وضعیت بازار؟"}),
                {"Content-Type": "application/json", "Origin": f"http://{self.host}:{self.port}"},
            )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["read_only"], True)
        self.assertFalse(json.loads(body)["orders_submitted"])
        chat.assert_called_once()
        self.assertEqual(chat.call_args.args[0], "وضعیت بازار؟")

    def test_ai_copilot_rejects_cross_origin_requests(self):
        with patch.object(web_app, "ai_copilot_chat") as chat:
            status, _, body = self.request(
                "POST", "/api/ai-copilot/chat", json.dumps({"message": "سلام"}),
                {"Content-Type": "application/json", "Origin": "https://attacker.example"},
            )
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["code"], "SAME_ORIGIN_REQUIRED")
        chat.assert_not_called()

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

    def test_challenge1_synthetic_dry_run_uses_single_groq_reviewer_and_never_trades(self):
        previous = dict(web_app.AI_DRY_RUN_STATE)
        second_config = {
            "configured": True, "provider": "Groq",
            "model": "openai/gpt-oss-20b", "status": "CONFIGURED",
            "configuration_issues": [],
        }
        groq_review = {
            "reviewer_id": "groq", "provider": "Groq",
            "model": "openai/gpt-oss-20b",
            "request_attempted": True, "response_source": "LIVE_API",
        }
        expected_quorum = {
            "approved": True,
            "status": "APPROVED_BY_GROQ",
            "reason_fa": "Synthetic test approval only.",
            "ai_consensus": {
                "status": "APPROVED_BY_GROQ",
                "decision": "AUTHORIZE_DEMO_EXECUTION_WITH_ONE_REVIEWER",
                "single_reviewer_approval": True,
                "reviewer_id": "groq",
                "reviewer_verified": True,
                "provider_is_groq": True,
                "model_is_groq_allowlisted": True,
                "reviewer_approved": True,
            },
            "ai_reviews": [
                {"reviewer_id": "groq", "actor": "Groq Supervisor", "review": groq_review},
            ],
            "ai_review": groq_review,
            "second_ai_review": None,
        }
        try:
            web_app.AI_DRY_RUN_STATE.update(day=datetime.now(timezone.utc).date().isoformat(), count=0, last_started=0.0)
            with (
                patch.object(web_app, "second_ai_connection_status", return_value=second_config),
                patch.object(web_app, "_run_challenge1_supervisor_reviews", return_value=expected_quorum) as reviewer,
                patch.object(web_app, "run_ai_supervisor") as generic_supervisor,
                patch.object(web_app.FUTURES_PAPER_BOT, "run_once") as futures_tick,
                patch.object(web_app.PAPER_BOT, "run_once") as spot_tick,
            ):
                result = web_app._run_ai_synthetic_dry_run_limited()
            self.assertTrue(result["test_passed"])
            self.assertEqual(result["test_mode"], "SYNTHETIC_CHALLENGE1_GROQ_DRY_RUN")
            self.assertEqual(result["ai_consensus"]["status"], "APPROVED_BY_GROQ")
            self.assertEqual(result["ai_consensus"]["reviewer_id"], "groq")
            self.assertEqual(len(result["ai_reviews"]), 1)
            self.assertEqual(result["ai_reviews"][0]["reviewer_id"], "groq")
            self.assertFalse(result["paper_entry_eligible"])
            self.assertFalse(result["orders_submitted"])
            self.assertFalse(result["trade_executed"])
            reviewer.assert_called_once()
            analysis, asset = reviewer.call_args.args
            self.assertEqual(asset.raw, "perp:BTCUSDT")
            self.assertEqual(analysis.metrics["execution_profile"], "CHALLENGE_1")
            record = analysis.to_record()
            self.assertEqual(record["challenge1_evidence"]["setup_timeframe"], "5m")
            self.assertEqual(record["challenge1_evidence"]["elliott_wave"]["phase"], "WAVES_1_AND_2_CONFIRMED")
            self.assertEqual(record["challenge1_evidence"]["chosen_pattern"]["name_en"], "Bullish Engulfing")
            generic_supervisor.assert_not_called()
            futures_tick.assert_not_called()
            spot_tick.assert_not_called()
        finally:
            web_app.AI_DRY_RUN_STATE.clear()
            web_app.AI_DRY_RUN_STATE.update(previous)

    def test_challenge1_synthetic_dry_run_fails_closed_when_groq_reviewer_is_missing(self):
        previous = dict(web_app.AI_DRY_RUN_STATE)
        second_config = {
            "configured": False, "provider": "Groq", "model": "openai/gpt-oss-20b",
            "status": "NOT_CONFIGURED", "configuration_issues": ["GROQ_API_KEY_MISSING"],
        }
        try:
            with (
                patch.object(web_app, "second_ai_connection_status", return_value=second_config),
                patch.object(web_app, "_run_challenge1_supervisor_reviews") as reviewer,
            ):
                result = web_app._run_ai_synthetic_dry_run_limited()
            self.assertEqual(result["status"], "NOT_CONFIGURED")
            self.assertFalse(result["test_passed"])
            self.assertFalse(result["paper_entry_eligible"])
            self.assertFalse(result["orders_submitted"])
            self.assertFalse(result["trade_executed"])
            self.assertEqual(len(result["ai_reviews"]), 1)
            self.assertEqual(result["ai_reviews"][0]["reviewer_id"], "groq")
            self.assertIs(result["ai_reviews"][0]["review"]["request_attempted"], False)
            reviewer.assert_not_called()
        finally:
            web_app.AI_DRY_RUN_STATE.clear()
            web_app.AI_DRY_RUN_STATE.update(previous)

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
        with web_app.CRYPTO_MARKET_CACHE_LOCK:
            web_app.CRYPTO_MARKET_CACHE.update(
                expires_at=0.0, payload=None, refreshing=False, last_error=None
            )

        def binance_ticker(symbol):
            return {"price": 100.0, "volume_24h": 1_000_000.0,
                    "raw": {"priceChangePercent": "2.0"}}

        def bybit_ticker(symbol):
            return {"price": 100.0, "volume_24h": 1_000_000.0,
                    "raw": {"price24hPcnt": "0.02"}}

        with patch.object(web_app.ENGINE.data.binance, "ticker", side_effect=binance_ticker) as binance_fetch, \
             patch.object(web_app.ENGINE.data.bybit, "ticker", side_effect=bybit_ticker) as bybit_fetch, \
             patch.object(web_app, "_market_volume_spike", return_value=1.5):
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
            self.assertEqual(first["items"][0]["price_change_24h_pct"], 2.0)
            self.assertEqual(first["items"][0]["providers"], ["Binance Spot", "Bybit Spot"])
            self.assertEqual(first["items"][0]["volume_spike_ratio"], 1.5)
            self.assertEqual(first["trade_authorization"], "NO_TRADE")

            status, _, payload = self.request("GET", "/api/crypto-markets")
            second = json.loads(payload)
            self.assertEqual(status, 200)
            self.assertEqual(second, first)
            self.assertEqual(binance_fetch.call_count, len(web_app.CRYPTO_MARKET_UNIVERSE))
            self.assertEqual(bybit_fetch.call_count, len(web_app.CRYPTO_MARKET_UNIVERSE))

    def test_crypto_market_endpoint_keeps_failed_symbols_unknown(self):
        with web_app.CRYPTO_MARKET_CACHE_LOCK:
            web_app.CRYPTO_MARKET_CACHE.update(
                expires_at=0.0, payload=None, refreshing=False, last_error=None
            )

        def binance_ticker(symbol):
            if symbol == "ETHUSDT":
                raise RuntimeError("public source unavailable")
            return {"price": 100.0, "volume_24h": 1_000_000.0,
                    "raw": {"priceChangePercent": "2.0"}}

        def bybit_ticker(symbol):
            if symbol == "ETHUSDT":
                raise RuntimeError("public source unavailable")
            return {"price": 100.0, "volume_24h": 1_000_000.0,
                    "raw": {"price24hPcnt": "0.02"}}

        with patch.object(web_app.ENGINE.data.binance, "ticker", side_effect=binance_ticker), \
             patch.object(web_app.ENGINE.data.bybit, "ticker", side_effect=bybit_ticker), \
             patch.object(web_app, "_market_volume_spike", return_value=None):
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

    def test_gem_hunter_pending_payload_includes_all_three_networks(self):
        result = web_app._gem_hunter_pending_payload(refreshing=True)
        self.assertEqual(set(result["networks"]), {"solana", "base", "ethereum"})
        self.assertEqual(result["trade_authorization"], "NO_TRADE")

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
        good.update({
            "pump_phase_fa": "🟢 فاز ۱: انباشت / شروع موج",
            "risk_score": 30,
            "qualification_status": "QUALIFIED_EARLY_WAVE",
            "trade_authorization": "NO_TRADE",
        })
        bad = dict(good)
        bad.update({
            "candidate_status": "UNSCREENED",
            "pump_phase_fa": "🔴 فاز ۳: سقف فرسودگی / خطر دامپ",
            "risk_score": 95,
            "qualification_status": "WATCH_ONLY_HIGH_RISK",
            "trade_authorization": "NO_TRADE",
        })
        with patch.object(web_app, "discover_gem_candidates", return_value={
            "status": "PASS", "candidate_count": 2,
            "candidates": [good, bad], "trade_authorization": "NO_TRADE",
        }), patch.object(web_app, "reconcile_discovery_candidates", side_effect=lambda rows, http: rows), \
             patch.object(web_app, "screen_discovery_candidates", side_effect=lambda rows, data: rows), \
             patch.object(web_app, "evaluate_discovery_candidates", side_effect=lambda rows: rows), \
             patch.object(web_app, "process_gem_hunter_v3_candidates", side_effect=lambda rows, notify: rows), \
             patch.object(web_app, "_start_gem_hunter_refresh", side_effect=web_app._refresh_gem_hunter_result):
            status, _, payload = self.request("GET", "/api/gem-hunter/discover?limit=20")
        result = json.loads(payload)
        self.assertEqual(status, 200)
        self.assertEqual(result["signal_status"], "EARLY_WAVE_CANDIDATES_FOUND")
        self.assertEqual(result["qualified_buy_count"], 0)
        self.assertEqual(result["qualified_early_wave_count"], 1)
        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(result["discovered_candidate_count"], 2)
        self.assertEqual(result["evaluation_record_count"], 2)
        self.assertEqual(result["complete_evaluation_count"], 2)
        self.assertEqual(result["unassessed_candidate_count"], 0)
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["candidates"][0]["trade_authorization"], "NO_TRADE")
        self.assertEqual(result["trade_authorization"], "NO_TRADE")
        self.assertEqual(result["candidates"][0]["qualification_status"], "QUALIFIED_EARLY_WAVE")
        self.assertNotIn("available_candidate_count", result)
        # The phase-3 twin is isolated in the warning-only bucket.

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
        self.assertEqual(result["signal_status"], "NO_QUALIFIED_BUY")
        self.assertEqual(result["qualification_engine_status"], "COMPLETE")
        self.assertEqual(result["discovered_candidate_count"], 1)
        self.assertEqual(result["evaluation_record_count"], 1)
        self.assertEqual(result["complete_evaluation_count"], 1)
        self.assertEqual(result["incomplete_evaluation_count"], 0)
        self.assertEqual(result["unassessed_candidate_count"], 0)
        self.assertIn("شواهد", result["diagnostic_fa"])
        self.assertEqual(result["qualified_buy_count"], 0)
        self.assertEqual(result["candidates"], [])
        self.assertFalse(result["public_policy"]["discovery_candidates_exposed"])
        self.assertNotIn("available_candidate_count", result)
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["warning_only_count"], 1)
        self.assertEqual(result["warning_only_candidates"][0]["qualification_status"], "WATCH_ONLY_HIGH_RISK")
        self.assertEqual(result["warning_only_candidates"][0]["trade_authorization"], "NO_TRADE")
        self.assertIn("61iusjsjCqfCW6SViHytSyYqvALUMcv3k2xMsUtM1UwP", json.dumps(result["warning_only_candidates"]))

    def test_gem_hunter_report_route_requests_v3_research_diagnostics_and_refresh(self):
        expected = {
            "status": "PASS",
            "diagnostic_candidates": [],
            "gem_hunter_v3_candidates": [],
            "gem_hunter_v3_tier1_count": 0,
            "gem_hunter_v3_tier2_count": 0,
            "trade_authorization": "NO_TRADE",
        }
        with patch.object(web_app, "_gem_hunter_api_payload", return_value=expected) as build:
            status, headers, payload = self.request(
                "GET", "/api/gem-hunter/report?limit=10&refresh=1",
            )
        result = json.loads(payload)
        self.assertEqual(status, 200)
        self.assertEqual(result["report_mode"], "RESEARCH_ONLY_NOT_A_TRADE_SIGNAL")
        self.assertEqual(result["trade_authorization"], "NO_TRADE")
        self.assertEqual(result["gem_hunter_v3_tier1_count"], 0)
        self.assertEqual(result["gem_hunter_v3_tier2_count"], 0)
        build.assert_called_once_with(limit=10, include_diagnostics=True, force_refresh=True)
        self.assertEqual(headers.get("Cache-Control"), "no-store")

    def test_gem_hunter_report_force_refresh_starts_a_fresh_scan_even_when_cache_is_valid(self):
        web_app.GEM_HUNTER_RESULT_CACHE.update(
            expires_at=web_app.time.monotonic() + 300,
            payload={"status": "PASS", "diagnostic_candidates": [], "candidates": []},
            refreshing=False, last_error=None,
        )

        def mark_refreshing():
            with web_app.GEM_HUNTER_RESULT_CACHE_LOCK:
                web_app.GEM_HUNTER_RESULT_CACHE["refreshing"] = True

        with patch.object(web_app, "_start_gem_hunter_refresh", side_effect=mark_refreshing) as start:
            result = web_app._gem_hunter_api_payload(limit=10, include_diagnostics=True, force_refresh=True)

        start.assert_called_once_with()
        self.assertTrue(result["refreshing"])
        self.assertEqual(result["signal_status"], "REFRESHING")
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["qualified_buy_count"], 0)

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


    def test_blofin_market_and_challenge1_is_the_only_demo_strategy_and_contract_catalog_is_live(self):
        status, _, home = self.request("GET", "/")
        page = home.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn('id="blofin-market-type"', page)
        self.assertIn('id="blofin-instrument"', page)
        self.assertIn('id="blofin-engine-profile"', page)
        self.assertIn("USDT Perpetual · Linear SWAP", page)
        profile_match = re.search(
            r'<select[^>]*id="blofin-engine-profile"[^>]*>(.*?)</select>',
            page,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(profile_match)
        profile_options = profile_match.group(1)
        self.assertIn('value="CHALLENGE_1"', profile_options)
        self.assertNotRegex(profile_options, r'value="(?:EASY|STANDARD|STRICT)"')
        self.assertIn("جهت LONG/SHORT فقط از سیگنال زنده صادر می‌شود", page)

        payload = {
            "status": "PASS",
            "market": "BloFin Demo USDT Perpetual (linear SWAP)",
            "count": 1,
            "instruments": [{
                "inst_id": "ETH-USDT", "base_currency": "ETH", "quote_currency": "USDT",
                "api_permission": "full", "min_size": 0.1,
                "lot_size": 0.1, "contract_value": 0.001, "tick_size": 0.1,
            }],
        }
        with patch.object(web_app, "_blofin_demo_instrument_options", return_value=payload) as options:
            catalog_status, _, catalog_body = self.request(
                "GET", "/api/blofin/demo/instruments?refresh=1",
            )
        self.assertEqual(catalog_status, 200)
        self.assertEqual(json.loads(catalog_body), payload)
        options.assert_called_once_with(force=True)

    def test_blofin_farsi_execution_report_identifies_selected_contract_and_strategy(self):
        report = web_app._blofin_execution_report_fa({
            "status": "completed",
            "stage": "PROFILE_GATE_REVIEW",
            "message_fa": "شرایط ورود هنوز کامل نیست.",
            "trade_context": {
                "instrument_id": "ETH-USDT",
                "execution_profile": "CHALLENGE_1",
                "analysis_time_utc": "2026-11-15T05:00:00+00:00",
            },
            "analysis": {
                "signal": "NO_TRADE",
                "gate_pass_count": 0,
                "gate_fail_count": 0,
                "gate_unknown_count": 12,
            },
            "result": {
                "status": "NO_TRADE",
                "orders_submitted": False,
                "trade_executed": False,
                "reason_fa": "الگوی معتبر پیدا نشد.",
            },
        })
        self.assertIn("ETH-USDT", report["title_fa"])
        self.assertEqual(report["instrument_id"], "ETH-USDT")
        self.assertEqual(report["execution_profile"], "CHALLENGE_1")
        self.assertIn("Challenge 1", report["stage_fa"])
        self.assertFalse(report["orders_submitted"])
        self.assertFalse(report["trade_executed"])

    def test_autopilot_next_scan_aligns_to_next_5m_close(self):
        now = datetime(2026, 11, 17, 16, 23, 32, tzinfo=timezone.utc)
        delay_seconds, next_at = web_app._blofin_demo_next_5m_scan_window(now)
        self.assertEqual(delay_seconds, 91)
        self.assertEqual(next_at, "2026-11-17T16:25:03+00:00")

    def test_autopilot_catches_candle_close_that_occurs_during_scan(self):
        started = datetime(2026, 11, 17, 17, 14, 22, tzinfo=timezone.utc)
        finished = datetime(2026, 11, 17, 17, 15, 43, tzinfo=timezone.utc)
        delay, next_at = web_app._blofin_demo_next_scan_after_cycle(
            started, now_utc=finished,
        )
        self.assertEqual(delay, 5)
        self.assertEqual(next_at, "2026-11-17T17:15:48+00:00")

    def test_autopilot_keeps_next_boundary_when_scan_finishes_before_it(self):
        started = datetime(2026, 11, 17, 17, 15, 5, tzinfo=timezone.utc)
        finished = datetime(2026, 11, 17, 17, 16, 20, tzinfo=timezone.utc)
        delay, next_at = web_app._blofin_demo_next_scan_after_cycle(
            started, now_utc=finished,
        )
        self.assertEqual(delay, 223)
        self.assertEqual(next_at, "2026-11-17T17:20:03+00:00")

    def test_blofin_autopilot_repeats_after_no_trade_with_spaced_signal(self):
        class StopAfterTwoCycles(Exception):
            pass

        old_state = dict(web_app.BLOFIN_AUTOPILOT_STATE)
        old_job_ids = set(web_app.BLOFIN_EXECUTION_JOBS)
        cycle_ids = []
        sleep_calls = []

        def record_job(job_id, status, **values):
            with web_app.LOCK:
                job = web_app.BLOFIN_EXECUTION_JOBS.setdefault(job_id, {})
                job.update({"status": status, **values})

        observed_profiles = []

        def complete_no_trade_cycle(job_id, instrument_id, execution_profile):
            cycle_ids.append(job_id)
            observed_profiles.append(execution_profile)
            self.assertEqual(execution_profile, "CHALLENGE_1")
            record_job(
                job_id,
                "completed",
                stage="NO_TRADE",
                message_fa="شرایط ورود تأیید نشد؛ سفارش ارسال نشد.",
                analysis={
                    "signal": web_app.Signal.NO_TRADE.value,
                    "gate_pass_count": 6,
                    "gate_fail_count": 0,
                    "gate_unknown_count": 6,
                },
                result={
                    "status": "NO_TRADE",
                    "orders_submitted": False,
                    "trade_executed": False,
                    "reason_fa": "سیگنال ورود معتبر وجود ندارد.",
                },
            )

        def stop_after_second_cycle_sleep(seconds):
            sleep_calls.append(seconds)
            if len(sleep_calls) == 2:
                raise StopAfterTwoCycles()

        web_app.BLOFIN_AUTOPILOT_STATE.update({
            "enabled": False,
            "status": "DISABLED",
            "cycles_started": 0,
        })
        try:
            with (
                patch.dict(os.environ, {
                    "BLOFIN_DEMO_AUTOPILOT_ENABLED": "true",
                    "BLOFIN_DEMO_AUTOPILOT_INTERVAL_SECONDS": "300",
                    "BLOFIN_DEMO_AUTOPILOT_INSTRUMENTS": "BTC-USDT",
                }),
                patch.object(web_app, "SCAN_RUNNING", False),
                patch.object(web_app, "_set_blofin_execution_job", side_effect=record_job),
                patch.object(web_app, "_run_blofin_demo_execution_job", side_effect=complete_no_trade_cycle),
                patch.object(web_app, "_blofin_demo_live_monitor_snapshot", return_value={
                    "status": "PASS",
                    "data_completeness": "COMPLETE",
                    "positions": [],
                    "closed_positions": [],
                    "read_errors": [],
                }),
                patch.object(web_app.time, "sleep", side_effect=stop_after_second_cycle_sleep),
                patch.object(
                    web_app, "_blofin_demo_next_scan_after_cycle",
                    return_value=(300, "2026-11-17T17:20:03+00:00"),
                ),
                patch.object(web_app, "_persist_blofin_autopilot_state", return_value=True),
            ):
                with self.assertRaises(StopAfterTwoCycles):
                    web_app._blofin_demo_autopilot_worker()

            self.assertEqual(len(cycle_ids), 2)
            self.assertEqual(observed_profiles, ["CHALLENGE_1", "CHALLENGE_1"])
            self.assertEqual(sleep_calls, [300, 300])
            self.assertEqual(web_app.BLOFIN_AUTOPILOT_STATE["cycles_started"], 2)
            self.assertEqual(web_app.BLOFIN_AUTOPILOT_STATE["status"], "WAITING_FOR_NEXT_5M_CANDLE")
            self.assertEqual(
                web_app.BLOFIN_AUTOPILOT_STATE["next_cycle_at_utc"],
                "2026-11-17T17:20:03+00:00",
            )
            self.assertTrue(web_app.BLOFIN_AUTOPILOT_STATE["search_continues"])
            self.assertTrue(web_app.BLOFIN_AUTOPILOT_STATE["next_cycle_scheduled"])
            self.assertEqual(web_app.BLOFIN_AUTOPILOT_STATE["last_cycle_status"], "completed")
            diagnostics = web_app.BLOFIN_AUTOPILOT_STATE["last_cycle_diagnostics"]
            self.assertEqual(diagnostics["signal"], "NO_TRADE")
            self.assertFalse(diagnostics["orders_submitted"])
            self.assertFalse(diagnostics["trade_executed"])
        finally:
            with web_app.LOCK:
                web_app.BLOFIN_AUTOPILOT_STATE.clear()
                web_app.BLOFIN_AUTOPILOT_STATE.update(old_state)
                for job_id in set(web_app.BLOFIN_EXECUTION_JOBS) - old_job_ids:
                    web_app.BLOFIN_EXECUTION_JOBS.pop(job_id, None)

    def test_startup_reconciliation_runs_even_when_scheduler_is_disabled(self):
        old_state = dict(web_app.BLOFIN_AUTOPILOT_STATE)
        old_done = web_app.BLOFIN_STARTUP_RECONCILE_DONE
        try:
            web_app.BLOFIN_STARTUP_RECONCILE_DONE = False
            with (
                patch.dict(os.environ, {
                    "BLOFIN_DEMO_AUTOPILOT_ENABLED": "false",
                    "BLOFIN_DEMO_AUTOPILOT_ROLE": "WEB",
                }, clear=False),
                patch.object(web_app, "_restore_blofin_autopilot_state"),
                patch.object(
                    web_app, "_schedule_blofin_demo_startup_recovery",
                ) as startup_reconcile,
                patch.object(web_app, "_persist_blofin_autopilot_state"),
            ):
                web_app._start_blofin_demo_autopilot()
            startup_reconcile.assert_called_once_with()
            self.assertEqual(web_app.BLOFIN_AUTOPILOT_STATE["status"], "DISABLED")
        finally:
            web_app.BLOFIN_STARTUP_RECONCILE_DONE = old_done
            web_app.BLOFIN_AUTOPILOT_STATE.clear()
            web_app.BLOFIN_AUTOPILOT_STATE.update(old_state)

    def test_startup_recovery_is_scheduled_as_a_daemon_thread(self):
        old_thread = web_app.BLOFIN_STARTUP_RECONCILE_THREAD
        old_done = web_app.BLOFIN_STARTUP_RECONCILE_DONE
        created = {}
        class FakeThread:
            def __init__(self, **kwargs):
                created.update(kwargs)
            def is_alive(self):
                return False
            def start(self):
                created["started"] = True
        try:
            web_app.BLOFIN_STARTUP_RECONCILE_DONE = False
            web_app.BLOFIN_STARTUP_RECONCILE_THREAD = None
            with patch.object(web_app.threading, "Thread", FakeThread):
                web_app._schedule_blofin_demo_startup_recovery()
            self.assertTrue(created["daemon"])
            self.assertTrue(created["started"])
            self.assertEqual(created["name"], "blofin-demo-startup-recovery")
            self.assertIs(created["target"], web_app._reconcile_blofin_demo_orders_on_startup)
        finally:
            web_app.BLOFIN_STARTUP_RECONCILE_THREAD = old_thread
            web_app.BLOFIN_STARTUP_RECONCILE_DONE = old_done

    def test_startup_reconciliation_fails_closed_without_durable_turso(self):
        old_state = dict(web_app.BLOFIN_AUTOPILOT_STATE)
        old_done = web_app.BLOFIN_STARTUP_RECONCILE_DONE
        try:
            web_app.BLOFIN_STARTUP_RECONCILE_DONE = False
            with (
                patch.object(web_app, "storage_backend", return_value="LOCAL_SQLITE"),
                patch.object(web_app, "_persist_blofin_autopilot_state"),
            ):
                result = web_app._reconcile_blofin_demo_orders_on_startup()
            self.assertEqual(result["status"], "UNAVAILABLE")
            self.assertFalse(result["orders_submitted"])
            self.assertEqual(result["error_type"], "RuntimeError")
            self.assertFalse(web_app.BLOFIN_AUTOPILOT_STATE["startup_reconciliation_orders_submitted"])
        finally:
            web_app.BLOFIN_STARTUP_RECONCILE_DONE = old_done
            web_app.BLOFIN_AUTOPILOT_STATE.clear()
            web_app.BLOFIN_AUTOPILOT_STATE.update(old_state)

    def test_startup_reconciliation_runs_durable_probe_then_read_only_sweep(self):
        old_state = dict(web_app.BLOFIN_AUTOPILOT_STATE)
        old_done = web_app.BLOFIN_STARTUP_RECONCILE_DONE
        class FakeStore:
            def __init__(self):
                self.closed = False
            def verify_durable_round_trip(self, *, connections):
                self.connections = connections
                return {
                    "verified": True, "connections": connections,
                    "writers_won": 1, "read_back": True, "cleanup": True,
                    "orders_submitted": False,
                }
            def close(self):
                self.closed = True
        class FakeExecutor:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
            def reconcile_unresolved_orders(self, *, limit):
                return {
                    "status": "RECONCILED", "attempted": 2,
                    "remaining_unresolved": 0, "orders_submitted": False,
                    "reconciliation": "READ_ONLY",
                }
        fake_store = FakeStore()
        try:
            web_app.BLOFIN_STARTUP_RECONCILE_DONE = False
            with (
                patch.object(web_app, "storage_backend", return_value="TURSO_CLOUD_SQLITE"),
                patch.object(web_app, "BloFinDemoEventStore", return_value=fake_store),
                patch.object(web_app, "BloFinDemoExecutor", FakeExecutor),
                patch.object(web_app, "_persist_blofin_autopilot_state"),
            ):
                result = web_app._reconcile_blofin_demo_orders_on_startup()
            self.assertEqual(result["status"], "RECONCILED")
            self.assertEqual(result["attempted"], 2)
            self.assertEqual(result["storage_probe"]["connections"], 4)
            self.assertFalse(result["orders_submitted"])
            self.assertTrue(fake_store.closed)
            self.assertFalse(web_app.BLOFIN_AUTOPILOT_STATE["startup_reconciliation_orders_submitted"])
        finally:
            web_app.BLOFIN_STARTUP_RECONCILE_DONE = old_done
            web_app.BLOFIN_AUTOPILOT_STATE.clear()
            web_app.BLOFIN_AUTOPILOT_STATE.update(old_state)

    def test_blofin_smoke_readiness_blocks_existing_open_position(self):
        with (
            patch.dict(os.environ, {"BLOFIN_DEMO_SMOKE_TEST_ENABLED": "true"}),
            patch.object(web_app, "_blofin_demo_execution_readiness", return_value={"status": "READY"}),
            patch.object(web_app, "_blofin_demo_live_monitor_snapshot", return_value={
                "status": "CONNECTED_READ_ONLY",
                "data_completeness": "COMPLETE",
                "read_errors": [],
                "positions": [{"instId": "BTC-USDT", "positions": "-0.1"}],
                "open_orders": [],
            }),
        ):
            result = web_app._blofin_demo_smoke_test_readiness()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("EXISTING_OPEN_POSITION", result["smoke_test_blockers"])
        self.assertEqual(result["smoke_test_open_position_count"], 1)
        self.assertEqual(result["smoke_test_open_order_count"], 0)

    def test_blofin_smoke_readiness_blocks_existing_pending_order(self):
        with (
            patch.dict(os.environ, {"BLOFIN_DEMO_SMOKE_TEST_ENABLED": "true"}),
            patch.object(web_app, "_blofin_demo_execution_readiness", return_value={"status": "READY"}),
            patch.object(web_app, "_blofin_demo_live_monitor_snapshot", return_value={
                "status": "CONNECTED_READ_ONLY",
                "data_completeness": "COMPLETE",
                "read_errors": [],
                "positions": [],
                "open_orders": [{"instId": "BTC-USDT", "orderId": "prior-order"}],
            }),
        ):
            result = web_app._blofin_demo_smoke_test_readiness()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("EXISTING_OPEN_ORDER", result["smoke_test_blockers"])
        self.assertEqual(result["smoke_test_open_position_count"], 0)
        self.assertEqual(result["smoke_test_open_order_count"], 1)

    def test_blofin_smoke_readiness_fails_closed_on_incomplete_monitor_data(self):
        with (
            patch.dict(os.environ, {"BLOFIN_DEMO_SMOKE_TEST_ENABLED": "true"}),
            patch.object(web_app, "_blofin_demo_execution_readiness", return_value={"status": "READY"}),
            patch.object(web_app, "_blofin_demo_live_monitor_snapshot", return_value={
                "status": "CONNECTED_READ_ONLY",
                "data_completeness": "PARTIAL",
                "read_errors": [{"endpoint": "positions", "error_type": "Timeout"}],
                "positions": [],
                "open_orders": [],
            }),
        ):
            result = web_app._blofin_demo_smoke_test_readiness()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("LIVE_MONITOR_INCOMPLETE", result["smoke_test_blockers"])
        self.assertIsNone(result["smoke_test_open_position_count"])

    def test_blofin_smoke_manual_account_check_runs_read_only_when_test_is_disabled(self):
        with (
            patch.dict(os.environ, {"BLOFIN_DEMO_SMOKE_TEST_ENABLED": "false"}),
            patch.object(web_app, "_blofin_demo_execution_readiness", return_value={
                "status": "BLOCKED", "message_fa": "مجوز Trade هنوز آماده نیست."
            }),
            patch.object(web_app, "_blofin_demo_live_monitor_snapshot", return_value={
                "status": "CONNECTED_READ_ONLY",
                "data_completeness": "COMPLETE",
                "read_errors": [],
                "positions": [],
                "open_orders": [],
                "positions_for_instrument": [],
                "open_orders_for_instrument": [],
            }) as read_snapshot,
        ):
            result = web_app._blofin_demo_smoke_test_readiness(check_account_state=True)

        read_snapshot.assert_called_once_with("BTC-USDT")
        self.assertEqual(result["status"], "BLOCKED")
        self.assertFalse(result["smoke_test_enabled"])
        self.assertTrue(result["read_only_account_check_performed"])
        self.assertEqual(result["read_only_account_status"], "CONNECTED_READ_ONLY")
        self.assertEqual(result["read_only_account_data_completeness"], "COMPLETE")
        self.assertEqual(result["smoke_test_open_position_count"], 0)
        self.assertEqual(result["smoke_test_open_order_count"], 0)
        self.assertFalse(result["read_only_account_orders_submitted"])

    def test_blofin_smoke_readiness_checks_btc_usdt_only(self):
        with (
            patch.dict(os.environ, {"BLOFIN_DEMO_SMOKE_TEST_ENABLED": "true"}),
            patch.object(web_app, "_blofin_demo_execution_readiness", return_value={"status": "READY"}),
            patch.object(web_app, "_blofin_demo_live_monitor_snapshot", return_value={
                "status": "CONNECTED_READ_ONLY",
                "data_completeness": "COMPLETE",
                "read_errors": [],
                "positions": [{"instId": "ETH-USDT", "positions": "0.5"}],
                "open_orders": [{"instId": "ETH-USDT", "orderId": "other-symbol-order"}],
                "positions_for_instrument": [],
                "open_orders_for_instrument": [],
            }),
        ):
            result = web_app._blofin_demo_smoke_test_readiness()

        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["smoke_test_open_position_count"], 0)
        self.assertEqual(result["smoke_test_open_order_count"], 0)

if __name__ == "__main__":
    unittest.main()

class Challenge1PositionManagerTests(unittest.TestCase):
    @staticmethod
    def order_and_position(*, pnl=0.75, mark_price=105.0, position_id="pos-1"):
        order = {
            "instrument_id": "BTC-USDT",
            "position_id": "pos-1",
            "client_order_id": "client-1",
            "side": "LONG",
            "take_profit": "110",
            "created_at": 1_000,
        }
        position = {
            "instId": "BTC-USDT",
            "positionId": position_id,
            "positions": "1",
            "unrealizedPnl": str(pnl),
        }
        return order, position, mark_price

    def test_profitable_position_continues_after_five_minutes_without_exit_trigger(self):
        order, position, mark_price = self.order_and_position(
            pnl=0.75, mark_price=105.0,
        )
        reason = web_app._blofin_demo_profit_exit_decision(
            order, position, mark_price, now=1_300,
            minimum_profit_usdt=0.50, maximum_hold_seconds=86_400,
        )
        self.assertIsNone(reason)

    def test_position_manager_exits_when_exact_linked_position_reaches_take_profit(self):
        order, position, mark_price = self.order_and_position(
            pnl=1.25, mark_price=110.0,
        )
        reason = web_app._blofin_demo_profit_exit_decision(
            order, position, mark_price, now=1_300,
            minimum_profit_usdt=0.50, maximum_hold_seconds=86_400,
        )
        self.assertEqual(reason, "TAKE_PROFIT_LEVEL_REACHED")

    def test_maximum_hold_exit_requires_profit_and_never_closes_a_loss(self):
        order, position, mark_price = self.order_and_position(
            pnl=0.75, mark_price=105.0,
        )
        reason = web_app._blofin_demo_profit_exit_decision(
            order, position, mark_price, now=87_400,
            minimum_profit_usdt=0.50, maximum_hold_seconds=86_400,
        )
        self.assertEqual(reason, "MAX_HOLD_PROFIT_ONLY")

        losing_order, losing_position, losing_mark = self.order_and_position(
            pnl=-0.25, mark_price=95.0,
        )
        losing_reason = web_app._blofin_demo_profit_exit_decision(
            losing_order, losing_position, losing_mark, now=87_400,
            minimum_profit_usdt=0.50, maximum_hold_seconds=86_400,
        )
        self.assertIsNone(losing_reason)

    def test_position_manager_requires_exact_position_id(self):
        order, position, mark_price = self.order_and_position(
            pnl=1.25, mark_price=115.0, position_id="different-position",
        )
        reason = web_app._blofin_demo_profit_exit_decision(
            order, position, mark_price, now=87_400,
            minimum_profit_usdt=0.50, maximum_hold_seconds=86_400,
        )
        self.assertIsNone(reason)

    def test_position_manager_thread_starts_independently_of_entry_autopilot(self):
        previous_thread = web_app.BLOFIN_POSITION_MANAGER_THREAD
        try:
            web_app.BLOFIN_POSITION_MANAGER_THREAD = None
            with (
                patch.dict(os.environ, {"BLOFIN_DEMO_AUTOPILOT_ENABLED": "false"}),
                patch.object(web_app.threading, "Thread") as thread_factory,
            ):
                worker = thread_factory.return_value
                worker.is_alive.return_value = False
                web_app._start_blofin_demo_position_manager()
                thread_factory.assert_called_once()
                self.assertIs(
                    thread_factory.call_args.kwargs["target"],
                    web_app._blofin_demo_position_manager_worker,
                )
                self.assertTrue(thread_factory.call_args.kwargs["daemon"])
                worker.start.assert_called_once()
        finally:
            web_app.BLOFIN_POSITION_MANAGER_THREAD = previous_thread



class Challenge1IndependentSupervisorGateTests(unittest.TestCase):
    def review(
        self, reviewer_id="groq", provider="Groq", model="openai/gpt-oss-20b", *,
        status="APPROVED", wave_phase="WAVES_1_AND_2_CONFIRMED",
        wave_direction="BULLISH", trader_proposal="LONG", paper_entry=True,
    ):
        return {
            "reviewer_id": reviewer_id,
            "provider": provider,
            "model": model,
            "returned_model": model,
            "status": status,
            "request_attempted": True,
            "response_source": "LIVE_API",
            "response_validated": True,
            "model_verified": True,
            "paper_entry_eligible": paper_entry,
            "expected_side": "LONG",
            "trader_proposal": trader_proposal,
            "elliott_wave_phase": wave_phase,
            "elliott_wave_direction": wave_direction,
        }

    def analysis(self):
        return SimpleNamespace(
            signal="LONG",
            metrics={
                "execution_profile": "CHALLENGE_1_V2",
                "challenge1_v2_evidence": {
                    "elliott_wave": {
                        "status": "CONFIRMED",
                        "pattern": "BULLISH",
                        "phase": "WAVES_1_AND_2_CONFIRMED",
                    },
                },
            },
        )

    def test_single_verified_groq_approval_authorizes_only_after_all_evidence_checks(self):
        groq = self.review()
        with (
            patch.object(web_app, "run_free_ai_second_opinion") as nemotron,
            patch.object(web_app, "run_independent_ai_supervisor", return_value=groq) as groq_reviewer,
        ):
            result = web_app._run_challenge1_supervisor_reviews(
                self.analysis(), "perp:BTCUSDT",
            )
        self.assertTrue(result["approved"])
        self.assertFalse(result["advisory_only"])
        self.assertEqual(result["status"], "APPROVED_BY_GROQ")
        self.assertEqual(result["ai_consensus"]["decision"], "AUTHORIZE_DEMO_EXECUTION_WITH_ONE_REVIEWER")
        self.assertTrue(result["ai_consensus"]["entry_gate_enforced"])
        self.assertTrue(result["ai_consensus"]["groq_approval_positive"])
        self.assertEqual(len(result["ai_reviews"]), 1)
        self.assertEqual(result["ai_reviews"][0]["reviewer_id"], "groq")
        nemotron.assert_not_called()
        groq_reviewer.assert_called_once()

    def test_groq_veto_blocks_entry_outside_demo_advisory_mode(self):
        groq = self.review(status="VETO", paper_entry=False)
        with patch.object(web_app, "run_independent_ai_supervisor", return_value=groq):
            result = web_app._run_challenge1_supervisor_reviews(
                self.analysis(), "perp:BTCUSDT",
            )
        self.assertFalse(result["approved"])
        self.assertFalse(result["advisory_only"])
        self.assertEqual(result["status"], "VETO")
        self.assertEqual(result["ai_consensus"]["decision"], "BLOCK_DEMO_ENTRY")
        self.assertTrue(result["ai_consensus"]["groq_vetoed"])
        self.assertEqual(len(result["ai_reviews"]), 1)

    def test_missing_or_unverified_groq_review_fails_closed(self):
        groq = {
            "reviewer_id": "groq", "provider": "Groq",
            "model": "openai/gpt-oss-20b", "status": "NOT_CONFIGURED",
            "request_attempted": False, "response_source": "NONE",
            "response_validated": False, "model_verified": False,
            "paper_entry_eligible": False,
        }
        with patch.object(web_app, "run_independent_ai_supervisor", return_value=groq):
            result = web_app._run_challenge1_supervisor_reviews(
                self.analysis(), "perp:BTCUSDT",
            )
        self.assertFalse(result["approved"])
        self.assertFalse(result["advisory_only"])
        self.assertEqual(result["ai_consensus"]["decision"], "BLOCK_DEMO_ENTRY")
        self.assertFalse(result["ai_consensus"]["groq_approval_positive"])
        self.assertEqual(len(result["ai_reviews"]), 1)

    def test_non_groq_provider_or_unallowlisted_model_cannot_approve(self):
        for changes in (
            {"provider": "OpenRouter"},
            {"model": "nvidia/nemotron-3-ultra-550b-a55b:free",
             "returned_model": "nvidia/nemotron-3-ultra-550b-a55b:free"},
        ):
            with self.subTest(changes=changes):
                groq = {**self.review(), **changes}
                with patch.object(web_app, "run_independent_ai_supervisor", return_value=groq):
                    result = web_app._run_challenge1_supervisor_reviews(
                        self.analysis(), "perp:BTCUSDT",
                    )
                self.assertFalse(result["approved"])
                self.assertFalse(result["advisory_only"])
                self.assertEqual(result["ai_consensus"]["decision"], "BLOCK_DEMO_ENTRY")

    def test_groq_wave_phase_mismatch_cannot_be_overridden_by_approval_flag(self):
        groq = self.review(wave_phase="WAVE_3_CONFIRMED")
        with patch.object(web_app, "run_independent_ai_supervisor", return_value=groq):
            result = web_app._run_challenge1_supervisor_reviews(
                self.analysis(), "perp:BTCUSDT",
            )
        self.assertFalse(result["approved"])
        self.assertFalse(result["advisory_only"])
        self.assertEqual(result["status"], "NOT_APPROVED")
        self.assertFalse(result["ai_consensus"]["groq_approval_positive"])



class BloFinUnifiedCredentialStatusTests(unittest.TestCase):
    def test_demo_connection_status_accepts_unified_account_credentials(self):
        class FakeBloFinReadOnly:
            def __init__(self, *, timeout):
                self.timeout = timeout

            def get_api_key_info(self):
                return {"readOnly": 1}

            def get_tickers(self, *, inst_id):
                self.assert_inst_id = inst_id
                return [{"instId": inst_id, "last": "100"}]

            def get_futures_balance(self, *, product_type):
                return {"productType": product_type, "balance": "0"}

            def get_positions(self):
                return []

            def get_open_orders(self):
                return []

        env = {
            "BLOFIN_API_KEY": "test-unified-key",
            "BLOFIN_API_SECRET": "test-unified-secret",
            "BLOFIN_PASSPHRASE": "test-unified-passphrase",
            "BLOFIN_DEMO_API_KEY": "",
            "BLOFIN_DEMO_API_SECRET": "",
            "BLOFIN_DEMO_PASSPHRASE": "",
        }
        previous_payload = web_app.BLOFIN_STATUS_CACHE.get("payload")
        previous_expiry = web_app.BLOFIN_STATUS_CACHE.get("expires_at", 0.0)
        try:
            web_app.BLOFIN_STATUS_CACHE.update(payload=None, expires_at=0.0)
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(web_app, "BloFinDemoReadOnly", FakeBloFinReadOnly),
            ):
                result = web_app._blofin_demo_connection_status()
            self.assertEqual(result["status"], "CONNECTED_READ_ONLY")
            self.assertEqual(result["api_key_permission"], "READ_ONLY")
            self.assertEqual(result["market_data"], "PASS")
            self.assertEqual(result["private_api"], "PASS")
            self.assertFalse(result["orders_enabled"])
        finally:
            web_app.BLOFIN_STATUS_CACHE.update(
                payload=previous_payload, expires_at=previous_expiry
            )


class BloFinUnifiedReadinessTests(unittest.TestCase):
    def test_unified_credential_source_is_reported_but_read_only_permission_stays_blocked(self):
        class FakeTradeClient:
            api_key = "test-unified-key"
            api_secret = "test-unified-secret"
            passphrase = "test-unified-passphrase"

            def __init__(self, *, timeout):
                self.timeout = timeout

            def verify_trade_permission(self):
                raise web_app.BloFinDemoExecutionBlocked("readOnly=1")

        class FakeEventStore:
            def trade_budget_status(self):
                return {
                    "orders_today": 0,
                    "orders_total": 0,
                    "unresolved_orders": 0,
                    "latest_order_status": None,
                    "latest_order_age_seconds": None,
                }

            def close(self):
                pass

        env = {
            "BLOFIN_API_KEY": "test-unified-key",
            "BLOFIN_API_SECRET": "test-unified-secret",
            "BLOFIN_PASSPHRASE": "test-unified-passphrase",
            "BLOFIN_DEMO_TRADE_API_KEY": "",
            "BLOFIN_DEMO_TRADE_API_SECRET": "",
            "BLOFIN_DEMO_TRADE_PASSPHRASE": "",
            "BLOFIN_DEMO_API_KEY": "",
            "BLOFIN_DEMO_API_SECRET": "",
            "BLOFIN_DEMO_PASSPHRASE": "",
            "BLOFIN_DEMO_TRADING_ENABLED": "false",
            "BLOFIN_DEMO_AUTOPILOT_ENABLED": "false",
            "BLOFIN_DASHBOARD_TOKEN": "",
        }
        with (
            patch.dict(os.environ, env, clear=False),
            patch.object(web_app, "BloFinDemoTradeClient", FakeTradeClient),
            patch.object(web_app, "storage_backend", return_value="LOCAL_SQLITE"),
            patch.object(web_app, "BloFinDemoEventStore", return_value=FakeEventStore()),
        ):
            result = web_app._blofin_demo_execution_readiness()

        self.assertTrue(result["trade_credentials_configured"])
        self.assertEqual(result["credential_source"], "UNIFIED_ACCOUNT_KEY")
        self.assertEqual(result["trade_permission_status"], "READ_ONLY")
        self.assertFalse(result["trade_permission_verified"])
        self.assertFalse(result["trading_enabled"])
        self.assertFalse(result["autopilot_enabled"])
        self.assertEqual(result["status"], "BLOCKED")

class BloFinPositionManagerReadinessReportingTests(unittest.TestCase):
    def test_position_manager_reports_actual_readiness_blockers_without_false_key_diagnosis(self):
        readiness = {
            "status": "BLOCKED",
            "trade_credentials_configured": True,
            "credential_source": "UNIFIED_ACCOUNT_KEY",
            "trade_permission_status": "READ_WRITE_CONFIRMED",
            "trade_permission_verified": True,
            "trading_enabled": False,
            "dashboard_auth_configured": False,
            "durable_storage": True,
            "event_store_readable": True,
        }
        with patch.object(
            web_app, "_blofin_demo_execution_readiness", return_value=readiness,
        ):
            result = web_app._blofin_demo_position_manager_cycle()

        self.assertEqual(result["status"], "EXECUTION_READINESS_BLOCKED")
        self.assertEqual(result["readiness_status"], "BLOCKED")
        self.assertEqual(
            result["readiness_blockers"],
            ["DEMO_TRADING_DISABLED", "DASHBOARD_TOKEN_NOT_CONFIGURED"],
        )
        self.assertEqual(result["trade_permission_status"], "READ_WRITE_CONFIRMED")
        self.assertEqual(result["credential_source"], "UNIFIED_ACCOUNT_KEY")
        self.assertFalse(result["trading_enabled"])
        self.assertFalse(result["dashboard_auth_configured"])
        self.assertTrue(result["durable_storage"])
        self.assertTrue(result["event_store_readable"])
        self.assertNotIn("کلید Trade اختصاصی", result["message_fa"])
        self.assertIn("فعال‌سازی معاملات Demo خاموش است", result["message_fa"])
        self.assertIn("توکن پنل تنظیم نشده است", result["message_fa"])


class BloFinDemoPositionEvidenceTests(unittest.TestCase):
    class FakeReadOnly:
        def __init__(self, candles=None, tpsl=None, candle_error=None, tpsl_error=None):
            self.candles = candles if candles is not None else []
            self.tpsl = tpsl if tpsl is not None else []
            self.candle_error = candle_error
            self.tpsl_error = tpsl_error

        def get_candles(self, **kwargs):
            if self.candle_error:
                raise self.candle_error
            return self.candles

        def get_open_tpsl_orders(self, **kwargs):
            if self.tpsl_error:
                raise self.tpsl_error
            return self.tpsl

    def test_market_direction_uses_only_recent_confirmed_closed_candles(self):
        now_ms = 1_800_000_000_000
        interval = 300_000
        first_stamp = now_ms - 3 * interval - 1_000
        closes = [100.0, 101.0, 102.0]
        rows = []
        previous = 99.5
        for index, close in enumerate(closes):
            rows.append([
                str(first_stamp + index * interval),
                str(previous),
                str(close + 0.5),
                str(previous - 0.5),
                str(close),
                "10",
                "1000",
                "0",
                "1",
            ])
            previous = close

        reader = self.FakeReadOnly(candles=rows)
        with patch.object(web_app.time, "time", return_value=now_ms / 1000):
            result = web_app._blofin_demo_position_market_evidence(reader, "BTC-USDT")

        self.assertEqual(result["status"], "VERIFIED")
        self.assertEqual(result["direction"], "UP")
        self.assertEqual(result["source"], "BLOFIN_CONFIRMED_5M_CANDLES")
        self.assertEqual(len(result["candles"]), 3)

    def test_market_direction_is_unknown_for_stale_or_unconfirmed_candles(self):
        now_ms = 1_800_000_000_000
        stale_stamp = now_ms - 12 * 300_000
        stale = [[str(stale_stamp + i * 300_000), "100", "102", "99", "101", "1", "1", "0", "1"] for i in range(3)]
        reader = self.FakeReadOnly(candles=stale)
        with patch.object(web_app.time, "time", return_value=now_ms / 1000):
            stale_result = web_app._blofin_demo_position_market_evidence(reader, "BTC-USDT")
        self.assertEqual(stale_result["status"], "STALE_CANDLES")
        self.assertEqual(stale_result["direction"], "UNKNOWN")

        fresh_unconfirmed = [
            [str(now_ms - 4 * 300_000 + i * 300_000), "100", "102", "99", "101", "1", "1", "0", "0"]
            for i in range(3)
        ]
        reader = self.FakeReadOnly(candles=fresh_unconfirmed)
        with patch.object(web_app.time, "time", return_value=now_ms / 1000):
            unconfirmed_result = web_app._blofin_demo_position_market_evidence(reader, "BTC-USDT")
        self.assertEqual(unconfirmed_result["status"], "NO_CONFIRMED_CANDLES")
        self.assertEqual(unconfirmed_result["direction"], "UNKNOWN")

    def test_protective_orders_require_exact_position_or_order_identity(self):
        row = {
            "tpslId": "tpsl-1",
            "clientOrderId": "client-1",
            "positionId": "position-1",
            "instId": "BTC-USDT",
            "positionSide": "long",
            "state": "live",
            "tpTriggerPrice": "110",
            "slTriggerPrice": "95",
            "tpOrderPrice": "-1",
            "slOrderPrice": "-1",
        }
        order = {"client_order_id": "client-1", "side": "LONG"}
        position = {"positionId": "position-1"}
        reader = self.FakeReadOnly(tpsl=[row])
        result = web_app._blofin_demo_protective_order_evidence(reader, "BTC-USDT", order, position)
        self.assertEqual(result["status"], "BOTH_TP_AND_SL_VISIBLE")
        self.assertEqual(result["matched_by"], ["EXACT_POSITION_ID"])

        order_only = dict(row)
        order_only.pop("positionId")
        reader = self.FakeReadOnly(tpsl=[order_only])
        result = web_app._blofin_demo_protective_order_evidence(reader, "BTC-USDT", order, position)
        self.assertEqual(result["status"], "BOTH_TP_AND_SL_VISIBLE")
        self.assertEqual(result["matched_by"], ["EXACT_CLIENT_ORDER_ID"])

        unrelated = dict(row, clientOrderId="some-other-order", positionId="some-other-position")
        reader = self.FakeReadOnly(tpsl=[unrelated])
        result = web_app._blofin_demo_protective_order_evidence(reader, "BTC-USDT", order, position)
        self.assertEqual(result["status"], "NOT_VERIFIED_NO_EXACT_POSITION_LINK")
        self.assertEqual(result["orders"], [])

    def test_stop_loss_threshold_crossings_are_detected_for_both_sides_without_order_side_effects(self):
        self.assertTrue(web_app._blofin_demo_stop_loss_triggered("LONG", 94.9, 95.0))
        self.assertFalse(web_app._blofin_demo_stop_loss_triggered("LONG", 95.1, 95.0))
        self.assertTrue(web_app._blofin_demo_stop_loss_triggered("SHORT", 105.1, 105.0))
        self.assertFalse(web_app._blofin_demo_stop_loss_triggered("SHORT", 104.9, 105.0))
        self.assertFalse(web_app._blofin_demo_stop_loss_triggered("LONG", float("nan"), 95.0))
        self.assertFalse(web_app._blofin_demo_stop_loss_triggered("UNKNOWN", 94.0, 95.0))


class BloFinOpenPositionObserverIntegrationTests(unittest.TestCase):
    def test_position_manager_records_pending_reviewer_skips_without_closing_position(self):
        now_ms = int(time.time() * 1000)
        interval_ms = 300_000
        latest_closed = (now_ms // interval_ms) * interval_ms - interval_ms
        candles = []
        previous = 100.0
        for index, close in enumerate((100.1, 100.2, 100.3)):
            candles.append([
                str(latest_closed - 2 * interval_ms + index * interval_ms),
                str(previous),
                str(close + 0.2),
                str(previous - 0.2),
                str(close),
                "12",
                "1200",
                "0",
                "1",
            ])
            previous = close

        order = {
            "client_order_id": "client-order-1",
            "exchange_order_id": "exchange-order-1",
            "instrument_id": "BTC-USDT",
            "position_id": "position-1",
            "experience_id": "experience-1",
            "side": "LONG",
            "created_at": int(time.time()) - 120,
            "entry_price": 100.0,
            "stop_loss": 95.0,
            "take_profit": 110.0,
        }
        position = {
            "positionId": "position-1",
            "instId": "BTC-USDT",
            "positions": "1",
            "positionSide": "long",
            "averagePrice": "100",
            "markPrice": "100.3",
            "unrealizedPnl": "0.20",
            "leverage": "2",
            "liquidationPrice": "50",
        }
        tpsl = {
            "tpslId": "tpsl-1",
            "clientOrderId": "client-order-1",
            "positionId": "position-1",
            "instId": "BTC-USDT",
            "positionSide": "long",
            "state": "live",
            "tpTriggerPrice": "110",
            "slTriggerPrice": "95",
            "tpOrderPrice": "-1",
            "slOrderPrice": "-1",
            "actualSize": "1",
        }
        snapshot = {
            "experience_id": "experience-1",
            "reason_fa": "Test entry snapshot",
            "actual_entry_price": 100.0,
            "evidence": {
                "setup_timeframe": "5m",
                "context_timeframe": "1H",
                "completed_candles_only": True,
                "decision_engine_version": "TEST",
                "detected_patterns": ["TEST_PATTERN"],
                "chart_structure": {"status": "VALID"},
                "candles_5m": [],
            },
        }
        fake_store = SimpleNamespace(
            orders_pending_learning_outcome=lambda **kwargs: [order],
            recent_events=lambda *args, **kwargs: [],
            close=lambda: None,
        )
        fake_reader = SimpleNamespace(
            get_positions=lambda: [position],
            get_mark_price=lambda **kwargs: [{"markPrice": "100.3"}],
            get_candles=lambda **kwargs: candles,
            get_open_tpsl_orders=lambda **kwargs: [tpsl],
        )
        fake_journal = SimpleNamespace(
            get_snapshot=lambda experience_id: snapshot,
            close=lambda: None,
        )
        reviewer_states = {
            reviewer_id: {
                "status": "PENDING",
                "reason_code": "REVIEW_BATCH_IN_FLIGHT",
                "request_attempted": False,
                "reason": "An earlier observer batch is still running.",
            }
            for reviewer_id in ("nemotron", "second_ai")
        }

        with (
            patch.object(
                web_app, "_blofin_demo_execution_readiness",
                return_value={"status": "READY"},
            ),
            patch.object(web_app, "BloFinDemoEventStore", return_value=fake_store),
            patch.object(web_app, "BloFinDemoReadOnly", return_value=fake_reader),
            patch.object(web_app, "NotionActivityJournal", return_value=fake_journal),
            patch.object(
                web_app, "schedule_position_observer_review",
                return_value=reviewer_states,
            ) as schedule,
            patch.object(
                web_app, "record_position_observer_skip",
                return_value={"status": "RECORDED"},
            ) as skip_event,
            patch.object(
                web_app, "record_position_monitor_cycle",
                return_value={
                    "status": "RECORDED",
                    "event_id": "monitor-cycle-event-1",
                    "minute_summaries": [],
                },
            ) as cycle_event,
            patch.object(web_app, "_blofin_manual_close_position") as close_position,
        ):
            result = web_app._blofin_demo_position_manager_cycle()

        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["managed_positions"], 1)
        schedule.assert_called_once()
        self.assertEqual(skip_event.call_count, 2)
        self.assertEqual(
            {call.kwargs["reviewer_id"] for call in skip_event.call_args_list},
            {"nemotron", "second_ai"},
        )
        self.assertTrue(all(
            call.kwargs["reason_code"] == "REVIEW_BATCH_IN_FLIGHT"
            for call in skip_event.call_args_list
        ))
        cycle_event.assert_called_once()
        cycle_details = cycle_event.call_args.kwargs["cycle_details"]
        self.assertEqual(cycle_details["decision"], "HOLD")
        self.assertEqual(cycle_details["heartbeat_status"], "ALIVE")
        self.assertEqual(cycle_details["market_direction"], "UP")
        self.assertEqual(
            cycle_details["protective_orders"]["status"],
            "BOTH_TP_AND_SL_VISIBLE",
        )
        self.assertEqual(
            cycle_details["reviewer_states"]["nemotron"]["status"],
            "PENDING",
        )
        self.assertEqual(
            cycle_details["reviewer_states"]["second_ai"]["status"],
            "PENDING",
        )
        self.assertEqual(
            result["actions"][0]["status"],
            "HOLD_UNTIL_PROFIT_EXIT",
        )
        close_position.assert_not_called()



class AiDryRunHttpDiagnosticsTests(unittest.TestCase):
    def test_provider_configuration_absence_is_service_unavailable_not_bad_gateway(self):
        self.assertEqual(
            web_app._ai_dry_run_http_status({"status": "PROVIDERS_INSUFFICIENT", "test_passed": False}),
            503,
        )
        self.assertEqual(
            web_app._ai_dry_run_http_status({"status": "MISSING_API_KEY", "test_passed": False}),
            503,
        )
        self.assertEqual(
            web_app._ai_dry_run_http_status({"status": "UNKNOWN", "test_passed": False}),
            502,
        )

    def test_rate_limits_keep_their_explicit_http_status(self):
        for status in ("RATE_LIMITED", "DRY_RUN_COOLDOWN", "DAILY_DRY_RUN_LIMIT"):
            with self.subTest(status=status):
                self.assertEqual(
                    web_app._ai_dry_run_http_status({"status": status, "test_passed": False}),
                    429,
                )

    def test_safe_diagnostic_keeps_reviewer_failure_metadata_but_excludes_secret_fields(self):
        payload = {
            "status": "PROVIDERS_INSUFFICIENT",
            "test_passed": False,
            "api_key": "never-log-this",
            "prompt": "never-log-this-either",
            "ai_reviews": [
                {
                    "reviewer_id": "nemotron",
                    "review": {
                        "status": "NOT_CONFIGURED",
                        "provider": "OpenRouter",
                        "model": "example-model",
                        "request_attempted": False,
                        "skip_reason_code": "OPENROUTER_NOT_CONFIGURED",
                        "api_key": "secret",
                    },
                },
                {
                    "reviewer_id": "second_ai",
                    "review": {
                        "status": "ERROR",
                        "provider": "Groq",
                        "model": "example-free-model",
                        "request_attempted": True,
                        "http_status": 401,
                        "latency_ms": 123,
                        "error_type": "AuthenticationError",
                        "raw_response": "sensitive response",
                    },
                },
            ],
            "ai_consensus": {
                "status": "UNKNOWN",
                "decision": "BLOCK_DEMO_ENTRY",
                "primary_verified": False,
                "second_verified": False,
                "api_key": "secret",
            },
            "orders_submitted": False,
            "trade_executed": False,
        }
        diagnostic = web_app._ai_dry_run_safe_diagnostic(payload)
        serialized = json.dumps(diagnostic)
        self.assertIn("OPENROUTER_NOT_CONFIGURED", serialized)
        self.assertIn("AuthenticationError", serialized)
        self.assertIn('"http_status": 401', serialized)
        self.assertNotIn("never-log-this", serialized)
        self.assertNotIn("sensitive response", serialized)
        self.assertNotIn('"api_key"', serialized)
        self.assertNotIn('"prompt"', serialized)




class BloFinRiskOverrideCloseTests(unittest.TestCase):
    def test_risk_override_closes_only_exact_negative_pnl_position(self):
        fake_client = Mock()
        fake_client.base_url = web_app.DEMO_REST_URL
        fake_client.get_positions.return_value = [{
            "instId": "BTC-USDT",
            "positionId": "pos-1",
            "positions": "1",
            "positionSide": "long",
            "marginMode": "isolated",
            "unrealizedPnl": "-0.75",
        }]
        fake_client.close_futures_position.return_value = {
            "data": {"orderId": "risk-close-order"},
        }
        fake_store = Mock()

        with (
            patch.object(
                web_app, "_blofin_demo_execution_readiness",
                return_value={"status": "READY"},
            ),
            patch.object(web_app, "BloFinDemoTradeClient", return_value=fake_client),
            patch.object(web_app, "BloFinDemoEventStore", return_value=fake_store),
        ):
            result = web_app._blofin_manual_close_position(
                instrument_id="BTC-USDT",
                position_id="pos-1",
                action_source="RISK_OVERRIDE",
                audit_id="client1",
                exit_reason="STOP_LOSS_CROSSED_WITHOUT_VERIFIED_SL",
            )

        self.assertEqual(result["status"], "CLOSE_ACCEPTED")
        self.assertTrue(result["loss_making_position"])
        fake_client.close_futures_position.assert_called_once_with(
            inst_id="BTC-USDT",
            margin_mode="isolated",
            position_side="long",
            client_order_id=ANY,
            position_id="pos-1",
        )
        self.assertEqual(fake_store.record_manual_action.call_count, 2)
        self.assertEqual(
            fake_store.record_manual_action.call_args_list[-1].args[1],
            "RISK_OVERRIDE_POSITION_CLOSE_ACCEPTED",
        )


class LiveRadarMultiMarketDashboardTests(unittest.TestCase):
    def test_custom_symbol_normalization_adds_usdt_and_preserves_pair(self):
        self.assertEqual(web_app._normalize_live_radar_symbol(" pepe "), "PEPEUSDT")
        self.assertEqual(web_app._normalize_live_radar_symbol("SHIBUSDT"), "SHIBUSDT")
        self.assertEqual(web_app._normalize_live_radar_symbol("sui/usdt"), "SUIUSDT")

    def test_custom_symbol_normalization_rejects_invalid_input(self):
        for value in ("", "BTC?USDT", "BTCUSDT;DROP", "THISISWAYTOOLONGSYMBOLUSDT"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    web_app._normalize_live_radar_symbol(value)

    def test_dashboard_renders_custom_search_and_five_market_watchlist(self):
        source = inspect.getsource(web_app)
        page = web_app._page(source)
        self.assertIn('id="live-radar-custom-symbol"', page.decode("utf-8"))
        self.assertIn('id="live-radar-custom-search"', page.decode("utf-8"))
        self.assertIn('id="live-wave-watch-grid"', page.decode("utf-8"))
        for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "LINKUSDT"):
            self.assertIn(symbol, page.decode("utf-8"))
        self.assertIn("refreshWaveWatchlist", page.decode("utf-8"))
        self.assertIn("نمایش روی نمودار بزرگ", page.decode("utf-8"))
