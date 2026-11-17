from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from crypto_super_radar import web_app


class FakeBloFinClient:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.calls = []

    def get_api_key_info(self):
        self.calls.append(("api_key_info", None))
        return {"readOnly": 1, "trade_permission": False}

    def get_tickers(self, *, inst_id=None):
        self.calls.append(("tickers", inst_id))
        return [{"instId": inst_id, "last": "50000"}]

    def get_futures_balance(self, *, product_type="USDT-FUTURES"):
        self.calls.append(("balance", product_type))
        return {"totalEquity": "5000", "private_marker": "must-not-leak"}

    def get_positions(self):
        self.calls.append(("positions", None))
        return [{"instId": "BTC-USDT", "positions": "0.1", "private_marker": "must-not-leak"}]

    def get_open_orders(self):
        self.calls.append(("orders", None))
        return [{"orderId": "private-order-id"}]


class FakeTradePermissionClient:
    permission_ok = True

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.api_key = "demo-key"
        self.api_secret = "demo-secret"
        self.passphrase = "demo-pass"

    def verify_trade_permission(self):
        if not self.permission_ok:
            raise web_app.BloFinDemoExecutionBlocked(
                "BloFin API key is read-only (readOnly=1); no write request was sent."
            )
        return {"readOnly": 0, "trade_permission": True}


class FakeReadinessStore:
    def trade_budget_status(self):
        return {"orders_today": 0, "orders_total": 0, "unresolved_orders": 0}

    def close(self):
        pass


class BloFinDashboardStatusTests(unittest.TestCase):
    def setUp(self):
        web_app.BLOFIN_STATUS_CACHE.update({"expires_at": 0.0, "payload": None})

    def tearDown(self):
        web_app.BLOFIN_STATUS_CACHE.update({"expires_at": 0.0, "payload": None})

    def test_execution_readiness_blocks_read_only_key_even_when_env_is_complete(self):
        values = {
            "BLOFIN_DEMO_TRADE_API_KEY": "demo-key",
            "BLOFIN_DEMO_TRADE_API_SECRET": "demo-secret",
            "BLOFIN_DEMO_TRADE_PASSPHRASE": "demo-pass",
            "BLOFIN_DEMO_TRADING_ENABLED": "true",
            "BLOFIN_DASHBOARD_TOKEN": "dashboard-token",
        }
        FakeTradePermissionClient.permission_ok = False
        with patch.dict(os.environ, values, clear=False), \
             patch.object(web_app, "BloFinDemoTradeClient", FakeTradePermissionClient), \
             patch.object(web_app, "storage_backend", return_value="TURSO_CLOUD_SQLITE"), \
             patch.object(web_app, "BloFinDemoEventStore", return_value=FakeReadinessStore()):
            result = web_app._blofin_demo_execution_readiness()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["trade_permission_status"], "READ_ONLY")
        self.assertFalse(result["trade_permission_verified"])
        self.assertFalse(result["trade_permission_verified_each_cycle"])

    def test_execution_readiness_requires_and_accepts_verified_read_write_key(self):
        values = {
            "BLOFIN_DEMO_TRADE_API_KEY": "demo-key",
            "BLOFIN_DEMO_TRADE_API_SECRET": "demo-secret",
            "BLOFIN_DEMO_TRADE_PASSPHRASE": "demo-pass",
            "BLOFIN_DEMO_TRADING_ENABLED": "true",
            "BLOFIN_DASHBOARD_TOKEN": "dashboard-token",
        }
        FakeTradePermissionClient.permission_ok = True
        with patch.dict(os.environ, values, clear=False), \
             patch.object(web_app, "BloFinDemoTradeClient", FakeTradePermissionClient), \
             patch.object(web_app, "storage_backend", return_value="TURSO_CLOUD_SQLITE"), \
             patch.object(web_app, "BloFinDemoEventStore", return_value=FakeReadinessStore()):
            result = web_app._blofin_demo_execution_readiness()
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["trade_permission_status"], "READ_WRITE_CONFIRMED")
        self.assertTrue(result["trade_permission_verified"])
        self.assertTrue(result["trade_permission_verified_each_cycle"])

    def test_missing_credentials_return_not_configured_without_api_call(self):
        with patch.dict(os.environ, {
            "BLOFIN_DEMO_API_KEY": "",
            "BLOFIN_DEMO_API_SECRET": "",
            "BLOFIN_DEMO_PASSPHRASE": "",
        }):
            with patch.object(web_app, "BloFinDemoReadOnly") as client:
                result = web_app._blofin_demo_connection_status()
        self.assertEqual(result["status"], "NOT_CONFIGURED")
        self.assertEqual(result["environment"], "BLOFIN_DEMO")
        self.assertFalse(result["orders_enabled"])
        client.assert_not_called()

    def test_success_requires_public_and_private_reads_but_returns_no_account_data(self):
        values = {
            "BLOFIN_DEMO_API_KEY": "demo-key-not-real",
            "BLOFIN_DEMO_API_SECRET": "demo-secret-not-real",
            "BLOFIN_DEMO_PASSPHRASE": "demo-pass-not-real",
        }
        fake = FakeBloFinClient()
        with patch.dict(os.environ, values):
            with patch.object(web_app, "BloFinDemoReadOnly", return_value=fake) as client:
                result = web_app._blofin_demo_connection_status()
        self.assertEqual(result["status"], "CONNECTED_READ_ONLY")
        self.assertEqual(result["market_data"], "PASS")
        self.assertEqual(result["private_api"], "PASS")
        self.assertEqual(result["api_key_permission"], "READ_ONLY")
        self.assertEqual(result["permission_mode"], "READ_ONLY_REQUESTS")
        self.assertEqual(result["request_scope"], "READ_ONLY_GET_REQUESTS")
        self.assertFalse(result["orders_enabled"])
        self.assertEqual(
            [call[0] for call in fake.calls],
            ["api_key_info", "tickers", "balance", "positions", "orders"],
        )
        self.assertEqual(client.call_args.kwargs, {"timeout": 8.0})
        serialized = str(result)
        self.assertNotIn("5000", serialized)
        self.assertNotIn("private_marker", serialized)
        self.assertNotIn("private-order-id", serialized)
        self.assertNotIn("demo-secret-not-real", serialized)

    def test_auth_failure_is_fail_closed_and_does_not_enable_orders(self):
        from crypto_super_radar.blofin_demo_readonly import BloFinDemoAuthError

        values = {
            "BLOFIN_DEMO_API_KEY": "demo-key-not-real",
            "BLOFIN_DEMO_API_SECRET": "demo-secret-not-real",
            "BLOFIN_DEMO_PASSPHRASE": "demo-pass-not-real",
        }
        with patch.dict(os.environ, values):
            with patch.object(web_app, "BloFinDemoReadOnly", side_effect=BloFinDemoAuthError("rejected")):
                result = web_app._blofin_demo_connection_status()
        self.assertEqual(result["status"], "AUTH_FAILED")
        self.assertFalse(result["orders_enabled"])
        self.assertNotIn("rejected", str(result))

    def test_challenge1_report_translates_raw_wave_evidence_even_when_no_trade(self):
        job = {
            "status": "completed",
            "stage": "NO_TRADE",
            "trade_context": {"instrument_id": "BTC-USDT", "execution_profile": "CHALLENGE_1"},
            "result": {
                "status": "NO_TRADE",
                "signal": "NO_TRADE",
                "elliott_wave": {
                    "status": "CANDIDATE",
                    "pattern": "BULLISH",
                    "phase": "WAVES_1_AND_2_CONFIRMED",
                    "wave_count": 2,
                    "next_wave": 3,
                    "invalidation_price": 97500.0,
                    "pivot_reversal_threshold_pct": 0.18,
                    "confirmed_pivot_count": 4,
                    "timeframe": "1H",
                    "source": "BLOFIN_DEMO_COMPLETED_1H_CANDLES",
                    "completed_candle_count": 65,
                    "pivots": [
                        {"kind": "LOW", "price": 97000.0, "timestamp": "2026-11-16T10:00:00+00:00",
                         "confirmed_at": "2026-11-16T11:00:00+00:00"}
                    ],
                },
                "orders_submitted": False,
                "trade_executed": False,
            },
        }
        report = web_app._blofin_execution_report_fa(job)
        wave = report["elliott_wave"]
        self.assertTrue(report["wave_analysis_applicable"])
        self.assertEqual(wave["status_fa"], "شمارش ساختاری موج نامزد است")
        self.assertEqual(wave["selected_wave_fa"].split("؛")[0], "سناریوی ساختاری منتخب: موج ۳ صعودی")
        self.assertEqual(wave["timeframe_fa"], "یک‌ساعته")
        self.assertEqual(wave["confirmed_pivot_count"], 4)
        self.assertIsNone(wave["invalidation_price_note_fa"])
        self.assertEqual(wave["pivots"][0]["kind_fa"], "کف تأییدشده")

    def test_invalid_wave_count_explains_missing_invalidation_price(self):
        report = web_app._blofin_elliott_wave_report_fa({
            "status": "AMBIGUOUS", "pattern": "AMBIGUOUS",
            "phase": "NO_RULE_VALID_IMPULSE",
            "invalidation_price": None,
            "pivot_reversal_threshold_pct": 0.18,
            "pivots": [{"kind": "LOW", "price": 100.0}],
        }, "NO_TRADE")
        self.assertIsNone(report["invalidation_price"])
        self.assertIn("شمارش معتبر", report["invalidation_price_note_fa"])
        self.assertIsNone(report["pivot_reversal_threshold_note_fa"])

    def test_independent_smoke_test_report_hides_elliott_wave_card(self):
        job = {
            "status": "completed",
            "stage": "CLOSED_VERIFIED",
            "test_name": "تست ربات در محیط دمو صرافی",
            "result": {
                "status": "CLOSED_VERIFIED",
                "environment": "BLOFIN_DEMO",
                "orders_submitted": True,
                "trade_executed": True,
                "position_closed": True,
            },
        }
        report = web_app._blofin_execution_report_fa(job)
        self.assertEqual(report["execution_profile"], "INDEPENDENT_DEMO_SMOKE_TEST")
        self.assertEqual(report["execution_profile_fa"], "تست مستقل Demo بدون سیگنال")
        self.assertFalse(report["wave_analysis_applicable"])
        self.assertIn("تحلیل موج الیوت", report["wave_analysis_note_fa"])
        self.assertEqual(report["elliott_wave"], {})
        self.assertIn("۱۲ فیلتر", report["gate_summary_fa"])



class MarketBoardFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.universe = (
            ("BTCUSDT", "Bitcoin", "بیت‌کوین"),
            ("ETHUSDT", "Ethereum", "اتریوم"),
        )

    def test_completed_but_all_unknown_attempts_are_not_fresh(self):
        with patch.object(web_app, "CRYPTO_MARKET_UNIVERSE", self.universe):
            result = web_app._market_board_payload(
                [{"symbol": "BTCUSDT", "status": "UNKNOWN"},
                 {"symbol": "ETHUSDT", "status": "UNKNOWN"}],
                refreshing=False,
            )
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["freshness"], "UNKNOWN")
        self.assertIsNone(result["updated_at_utc"])

    def test_partial_quotes_report_oldest_successful_observation(self):
        first = datetime(2026, 11, 17, 0, 0, tzinfo=timezone.utc)
        second = first + timedelta(seconds=20)
        items = [
            {"symbol": "BTCUSDT", "status": "PASS", "received_at_utc": first.isoformat()},
            {"symbol": "ETHUSDT", "status": "UNKNOWN"},
        ]
        with patch.object(web_app, "CRYPTO_MARKET_UNIVERSE", self.universe):
            result = web_app._market_board_payload(items, refreshing=False)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["freshness"], "PARTIAL")
        self.assertEqual(result["updated_at_utc"], first.isoformat())

    def test_all_quotes_need_valid_timestamps_before_board_can_pass(self):
        first = datetime(2026, 11, 17, 0, 0, tzinfo=timezone.utc)
        second = first + timedelta(seconds=20)
        items = [
            {"symbol": "BTCUSDT", "status": "PASS", "received_at_utc": first.isoformat()},
            {"symbol": "ETHUSDT", "status": "PASS", "received_at_utc": second.isoformat()},
        ]
        with patch.object(web_app, "CRYPTO_MARKET_UNIVERSE", self.universe):
            result = web_app._market_board_payload(items, refreshing=False)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["freshness"], "FRESH")
        self.assertEqual(result["updated_at_utc"], first.isoformat())
        health = web_app.build_feed_health_report(
            result, now=first + timedelta(seconds=70)
        )
        self.assertEqual(health["status"], "STALE")
        self.assertEqual(health["age_seconds"], 70.0)

    def test_missing_one_row_timestamp_prevents_full_board_pass(self):
        first = datetime(2026, 11, 17, 0, 0, tzinfo=timezone.utc)
        items = [
            {"symbol": "BTCUSDT", "status": "PASS", "received_at_utc": first.isoformat()},
            {"symbol": "ETHUSDT", "status": "PASS"},
        ]
        with patch.object(web_app, "CRYPTO_MARKET_UNIVERSE", self.universe):
            result = web_app._market_board_payload(items, refreshing=False)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["freshness"], "PARTIAL")

if __name__ == "__main__":
    unittest.main()
