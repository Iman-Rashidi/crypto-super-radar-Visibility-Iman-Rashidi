from __future__ import annotations

import unittest
from unittest.mock import patch

from crypto_super_radar import web_app


class FakeBloFinAccountClient:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def get_futures_balance(self, *, product_type="USDT-FUTURES"):
        return {
            "ts": "1700000000000",
            "totalEquity": "1234.50",
            "isolatedEquity": "10",
            "details": [{
                "currency": "USDT",
                "equity": "1234.50",
                "balance": "1200",
                "available": "1100",
                "private_marker": "must-not-leak",
            }],
            "private_marker": "must-not-leak",
        }

    def get_positions(self):
        return [{
            "instId": "BTC-USDT",
            "positionSide": "long",
            "positions": "0.01",
            "unrealizedPnl": "2.5",
            "private_marker": "must-not-leak",
        }]

    def get_open_orders(self):
        return [{
            "instId": "ETH-USDT",
            "side": "buy",
            "orderType": "limit",
            "price": "2000",
            "size": "0.1",
            "state": "live",
            "private_marker": "must-not-leak",
        }]


class BloFinDashboardAccountTests(unittest.TestCase):
    def test_bearer_token_validation_fails_closed(self):
        self.assertTrue(web_app._blofin_dashboard_token_valid("Bearer secret-token", "secret-token"))
        self.assertFalse(web_app._blofin_dashboard_token_valid("", "secret-token"))
        self.assertFalse(web_app._blofin_dashboard_token_valid("Basic secret-token", "secret-token"))
        self.assertFalse(web_app._blofin_dashboard_token_valid("Bearer wrong", "secret-token"))
        self.assertFalse(web_app._blofin_dashboard_token_valid("Bearer secret-token", ""))

    def test_account_snapshot_only_returns_whitelisted_fields(self):
        with patch.object(web_app, "BloFinDemoReadOnly", FakeBloFinAccountClient):
            result = web_app._blofin_demo_account_snapshot()
        self.assertEqual(result["status"], "CONNECTED_READ_ONLY")
        self.assertFalse(result["orders_enabled"])
        self.assertEqual(result["balance"]["totalEquity"], "1234.50")
        self.assertEqual(result["positions"][0]["instId"], "BTC-USDT")
        self.assertEqual(result["open_orders"][0]["instId"], "ETH-USDT")
        self.assertNotIn("private_marker", str(result))


if __name__ == "__main__":
    unittest.main()
