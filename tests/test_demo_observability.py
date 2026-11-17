from __future__ import annotations

import json
import unittest

from crypto_super_radar.demo_observability import (
    build_closed_position_event,
    build_fill_event,
    build_heartbeat_event,
    build_manual_action_event,
)


class DemoObservabilityEventTests(unittest.TestCase):
    def test_exchange_fill_event_is_idempotent_and_not_a_closed_trade(self):
        fill = {
            "tradeId": "trade-001",
            "orderId": "order-001",
            "instId": "BTC-USDT",
            "side": "buy",
            "fillPrice": "90000.1",
            "fillSize": "0.01",
            "fillPnl": "0",
            "fee": "-0.02",
            "ts": "2026-11-17T11:10:00Z",
        }
        event = build_fill_event("BTC-USDT", fill)
        self.assertEqual(event["event_type"], "ORDER_FILLED")
        self.assertEqual(event["event_id"], "blofin:fill:BTC-USDT:trade-001")
        self.assertEqual(event["occurred_at_utc"], "2026-11-17T11:10:00.000Z")
        self.assertEqual(event["actor"], "BloFin Demo")
        self.assertNotEqual(event["event_type"], "POSITION_CLOSED")
        self.assertIn("order-001", event["details"])

    def test_closed_position_event_uses_exchange_pnl_and_history_identity(self):
        row = {
            "historyId": "history-77",
            "positionId": "position-88",
            "instId": "ETH-USDT",
            "side": "buy",
            "closePositions": "0.2",
            "maxPositions": "0.2",
            "openAveragePrice": "3000",
            "closeAveragePrice": "3100",
            "createTime": "1794910000000",
            "updateTime": "1794913600000",
            "realizedPnl": "20",
            "fee": "-0.1",
        }
        event = build_closed_position_event("ETH-USDT", row)
        self.assertEqual(event["event_type"], "POSITION_CLOSED")
        self.assertEqual(event["event_id"], "blofin:position-closed:ETH-USDT:history-77")
        self.assertEqual(event["position_id"], "position-88")
        detail = json.loads(event["details"])
        self.assertEqual(detail["realizedPnl"], "20")
        self.assertEqual(detail["closeAveragePrice"], "3100")

    def test_manual_close_is_logged_as_pending_not_as_final_close(self):
        event = build_manual_action_event(
            "/api/blofin/demo/manual-close-position",
            {
                "status": "CLOSE_ACCEPTED",
                "instrument_id": "XRP-USDT",
                "position_id": "position-1",
                "unrealized_pnl_usdt_at_confirmation": "0.09",
            },
        )
        self.assertEqual(event["event_type"], "HEARTBEAT")
        self.assertEqual(event["status"], "PENDING")
        self.assertEqual(event["position_id"], "position-1")
        self.assertIn("پس از تطبیق history", event["reason"])

    def test_manual_open_is_reported_as_submission_until_exchange_fill_is_confirmed(self):
        event = build_manual_action_event(
            "/api/blofin/demo/manual-open",
            {
                "status": "ACCEPTED",
                "instrument_id": "BTC-USDT",
                "exchange_order_id": "order-55",
                "client_order_id": "MANUAL-55",
            },
        )
        self.assertEqual(event["event_type"], "ORDER_SUBMITTED")
        self.assertEqual(event["status"], "SUCCESS")
        self.assertEqual(event["order_id"], "order-55")

    def test_heartbeat_identifies_its_snapshot_as_read_only(self):
        event = build_heartbeat_event(
            positions=[{
                "instId": "XRP-USDT", "positions": "0.1",
                "unrealizedPnl": "0.09", "averagePrice": "0.95",
            }],
            symbols=["BTC-USDT", "XRP-USDT"],
            status="OK",
            fills_observed=2,
            closed_positions_observed=1,
            errors=[],
            bucket=599000,
        )
        self.assertEqual(event["event_type"], "HEARTBEAT")
        self.assertEqual(event["status"], "SUCCESS")
        details = json.loads(event["details"])
        self.assertTrue(details["read_only"])
        self.assertEqual(details["open_position_count"], 1)
        self.assertEqual(details["closed_positions_observed_in_cycle"], 1)


if __name__ == "__main__":
    unittest.main()
