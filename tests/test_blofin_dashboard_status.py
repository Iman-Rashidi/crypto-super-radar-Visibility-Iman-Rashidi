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


class BloFinDashboardStatusTests(unittest.TestCase):
    def setUp(self):
        web_app.BLOFIN_STATUS_CACHE.update({"expires_at": 0.0, "payload": None})

    def tearDown(self):
        web_app.BLOFIN_STATUS_CACHE.update({"expires_at": 0.0, "payload": None})

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
