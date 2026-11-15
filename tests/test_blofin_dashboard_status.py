from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from crypto_super_radar import web_app


class FakeBloFinClient:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.calls = []

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
        self.assertFalse(result["orders_enabled"])
        self.assertEqual(
            [call[0] for call in fake.calls],
            ["tickers", "balance", "positions", "orders"],
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


if __name__ == "__main__":
    unittest.main()
