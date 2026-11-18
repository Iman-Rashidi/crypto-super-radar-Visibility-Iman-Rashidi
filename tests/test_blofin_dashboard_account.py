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



class FakeBloFinMonitorClient:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def get_tickers(self, *, inst_id=None):
        return [{"instId": "BTC-USDT", "last": "65311.6", "ts": "1800000000000"}]

    def get_mark_price(self, *, inst_id):
        return [{"instId": inst_id, "markPrice": "65310.8", "indexPrice": "65305.2", "ts": "1800000000100"}]

    def get_futures_balance(self, *, product_type="USDT-FUTURES"):
        return {
            "ts": "1800000000000",
            "totalEquity": "0.000000000000000000",
            "isolatedEquity": "0",
            "details": [{
                "currency": "USDT", "balance": "0", "equity": "0",
                "available": "0", "availableEquity": "0", "frozen": "0"
            }]
        }

    def get_positions(self, *, inst_id=None):
        return [{
            "instId": "BTC-USDT", "positionSide": "long", "positions": "0.01",
            "averagePrice": "65000", "markPrice": "65310.8",
            "unrealizedPnl": "3.1", "leverage": "1", "updateTime": "1800000000000"
        }]

    def get_open_orders(self, *, inst_id=None):
        return [{"orderId": "open-1", "side": "sell", "orderType": "limit", "state": "live"}]

    def get_open_tpsl_orders(self, *, inst_id=None, limit=100):
        return [{
            "tpslId": "tpsl-1", "side": "sell", "tpTriggerPrice": "67000",
            "slTriggerPrice": "64000", "state": "live"
        }]

    def get_tpsl_orders_history(self, *, inst_id=None, limit=100):
        return [{"tpslId": "tpsl-old", "state": "effective", "updateTime": "1799999999000"}]

    def get_fills_history(self, *, inst_id, limit=100):
        return [{"tradeId": "fill-1", "orderId": "order-1", "side": "buy",
                 "fillPrice": "65000", "fillSize": "0.01", "ts": "1800000000000"}]

    def get_orders_history(self, *, inst_id, limit=100):
        return [{"orderId": "order-1", "state": "filled", "averagePrice": "65000",
                 "filledSize": "0.01", "createTime": "1799999998000",
                 "updateTime": "1800000000000", "orderCategory": "normal"}]

    def get_positions_history(self, *, inst_id, state=None, limit=100):
        return [{"historyId": "hist-1", "side": "sell", "openAveragePrice": "65000",
                 "closeAveragePrice": "66000", "realizedPnl": "10",
                 "createTime": "1799999998000", "updateTime": "1800000000000"}]


class FakeBloFinMonitorStore:
    def latest_order(self):
        return None

    def recent_all_events(self, *, limit=100):
        return [{"sequence": 1, "event_type": "FILLED", "client_order_id": "bot-order-1",
                 "occurred_at": 1800000000, "payload": {"fill_price": "65000"}}]

    def close(self):
        pass


class BloFinLiveMonitorSnapshotTests(unittest.TestCase):
    def test_live_snapshot_includes_mark_equity_fills_completed_orders_and_tpsl(self):
        with patch.object(web_app, "BloFinDemoReadOnly", FakeBloFinMonitorClient), patch.object(web_app, "BloFinDemoEventStore", FakeBloFinMonitorStore):
            result = web_app._blofin_demo_live_monitor_snapshot()
        self.assertEqual(result["status"], "CONNECTED_READ_ONLY")
        self.assertEqual(result["data_completeness"], "COMPLETE")
        self.assertFalse(result["orders_enabled"])
        self.assertEqual(result["market"]["markPrice"], "65310.8")
        self.assertEqual(result["account"]["totalEquity"], "0.000000000000000000")
        self.assertEqual(result["account"]["details"][0]["available"], "0")
        self.assertEqual(result["fills"][0]["tradeId"], "fill-1")
        self.assertEqual(result["completed_orders"][0]["orderId"], "order-1")
        self.assertEqual(result["open_tpsl_orders"][0]["tpTriggerPrice"], "67000")
        self.assertEqual(result["tpsl_orders_history"][0]["tpslId"], "tpsl-old")
        self.assertEqual(result["execution_events"][0]["client_order_id"], "bot-order-1")

if __name__ == "__main__":
    unittest.main()
