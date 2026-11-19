from __future__ import annotations

import json
import unittest

from crypto_super_radar.blofin_demo_private_websocket import (
    BloFinDemoPrivateWebSocket,
    PRIVATE_CHANNELS,
)


class FakeReader:
    def websocket_urls(self):
        return {
            "public": "wss://demo-trading-openapi.blofin.com/ws/public",
            "private": "wss://demo-trading-openapi.blofin.com/ws/private",
        }

    def websocket_login_args(self):
        return {
            "apiKey": "secret-api-key",
            "passphrase": "secret-passphrase",
            "timestamp": "1700000000000",
            "sign": "secret-signature",
            "nonce": "test-nonce",
        }


class FakeWebSocket:
    def __init__(self, messages=()):
        self.messages = list(messages)
        self.sent = []

    def send(self, payload):
        self.sent.append(payload)

    def recv(self, timeout=None):
        if not self.messages:
            raise TimeoutError("no fake message")
        return self.messages.pop(0)


class BloFinDemoPrivateWebSocketTests(unittest.TestCase):
    def make_client(self, **kwargs):
        return BloFinDemoPrivateWebSocket(
            FakeReader(),
            reconcile_callback=lambda: {
                "positions": [{
                    "positionId": "1001",
                    "instId": "BTC-USDT",
                    "positions": "2",
                    "unrealizedPnl": "1.5",
                }],
                "orders": [{
                    "orderId": "2001",
                    "clientOrderId": "entry001",
                    "state": "live",
                }],
                "algo_orders": [{
                    "tpslId": "3001",
                    "positionId": "1001",
                    "instId": "BTC-USDT",
                    "state": "live",
                    "slTriggerPrice": "98",
                    "tpTriggerPrice": "105",
                }],
                "account": [{"equity": "1000", "available": "800"}],
            },
            **kwargs,
        )

    def test_only_private_demo_channels_are_declared(self):
        self.assertEqual(
            PRIVATE_CHANNELS,
            ("positions", "orders", "orders-algo", "account"),
        )
        urls = FakeReader().websocket_urls()
        self.assertEqual(
            urls["private"],
            "wss://demo-trading-openapi.blofin.com/ws/private",
        )
        self.assertIn("demo-trading-openapi.blofin.com", urls["private"])

    def test_login_ack_and_all_subscription_acks_are_required(self):
        messages = [
            json.dumps({"event": "login", "code": "0"}),
            *[
                json.dumps({"event": "subscribe", "arg": {"channel": channel}})
                for channel in PRIVATE_CHANNELS
            ],
        ]
        websocket = FakeWebSocket(messages)
        client = self.make_client()
        client._wait_for_event(websocket, expected_event="login")
        client._wait_for_subscriptions(websocket)
        self.assertTrue(client.get_snapshot()["authenticated"] is False)
        self.assertEqual(
            set(client.get_snapshot()["subscribed_channels"]),
            set(PRIVATE_CHANNELS),
        )

    def test_subscription_acknowledgements_may_arrive_out_of_order(self):
        messages = [
            json.dumps({"event": "subscribe", "arg": {"channel": channel}, "code": "0"})
            for channel in ("account", "orders-algo", "orders", "positions")
        ]
        websocket = FakeWebSocket(messages)
        client = self.make_client()
        client._wait_for_subscriptions(websocket)
        self.assertEqual(
            set(client.get_snapshot()["subscribed_channels"]),
            set(PRIVATE_CHANNELS),
        )

    def test_subscription_wait_preserves_data_events_before_last_ack(self):
        websocket = FakeWebSocket([
            json.dumps({
                "action": "update",
                "arg": {"channel": "positions"},
                "data": [{"positionId": "1001", "instId": "BTC-USDT", "positions": "3"}],
            }),
            json.dumps({"event": "subscribe", "arg": {"channel": "account"}, "code": "0"}),
            json.dumps({"event": "subscribe", "arg": {"channel": "orders"}, "code": "0"}),
            json.dumps({"event": "subscribe", "arg": {"channel": "positions"}, "code": "0"}),
            json.dumps({"event": "subscribe", "arg": {"channel": "orders-algo"}, "code": "0"}),
        ])
        client = self.make_client()
        client._wait_for_subscriptions(websocket)
        self.assertEqual(
            client.get_snapshot()["positions"]["1001"]["positions"],
            "3",
        )

    def test_login_rejection_fails_closed(self):
        client = self.make_client()
        websocket = FakeWebSocket([
            json.dumps({"event": "login", "code": "60009"}),
        ])
        with self.assertRaises(RuntimeError):
            client._wait_for_event(websocket, expected_event="login")

    def test_reconciliation_seeds_positions_orders_algo_orders_and_account(self):
        client = self.make_client()
        client._seed_reconciliation(client.reconcile_callback())
        snapshot = client.get_snapshot()
        self.assertTrue(snapshot["reconciled"])
        self.assertIn("1001", snapshot["positions"])
        self.assertIn("2001", snapshot["orders"])
        self.assertIn("3001", snapshot["algo_orders"])
        self.assertEqual(snapshot["account"][0]["equity"], "1000")

    def test_reconciliation_rejects_missing_private_snapshots(self):
        client = self.make_client(reconcile_callback=lambda: {
            "positions": [],
            "orders": [],
            "algo_orders": [],
            "account": None,
        })
        with self.assertRaises(ValueError):
            client._seed_reconciliation(client.reconcile_callback())
        self.assertFalse(client.get_snapshot()["reconciled"])

    def test_positions_snapshot_replaces_baseline_and_updates_merge_by_position_id(self):
        client = self.make_client()
        client._seed_reconciliation(client.reconcile_callback())
        client._process_json_message({
            "action": "snapshot",
            "arg": {"channel": "positions"},
            "data": [{
                "positionId": "1002",
                "instId": "ETH-USDT",
                "positions": "1",
            }],
        })
        snapshot = client.get_snapshot()
        self.assertEqual(set(snapshot["positions"]), {"1002"})
        client._process_json_message({
            "action": "update",
            "arg": {"channel": "positions"},
            "data": [{
                "positionId": "1002",
                "instId": "ETH-USDT",
                "positions": "0",
            }],
        })
        closed = client.get_snapshot()["positions"]["1002"]
        self.assertEqual(closed["positions"], "0")

    def test_orders_and_algo_updates_merge_with_rest_reconciliation(self):
        client = self.make_client()
        client._seed_reconciliation(client.reconcile_callback())
        client._process_json_message({
            "action": "update",
            "arg": {"channel": "orders"},
            "data": [{"orderId": "2001", "state": "filled", "filledSize": "2"}],
        })
        client._process_json_message({
            "action": "update",
            "arg": {"channel": "orders-algo"},
            "data": [{"tpslId": "3001", "state": "effective", "slTriggerPrice": "99"}],
        })
        snapshot = client.get_snapshot()
        self.assertEqual(snapshot["orders"]["2001"]["state"], "filled")
        self.assertEqual(snapshot["algo_orders"]["3001"]["state"], "effective")
        self.assertEqual(snapshot["algo_orders"]["3001"]["tpTriggerPrice"], "105")

    def test_ping_frame_receives_application_level_pong(self):
        client = self.make_client()
        websocket = FakeWebSocket()
        client._process_raw(websocket, "ping")
        self.assertEqual(websocket.sent, ["pong"])

    def test_snapshot_is_a_defensive_copy_and_contains_no_credentials(self):
        client = self.make_client()
        snapshot = client.get_snapshot()
        snapshot["positions"]["fake"] = {"positions": "999"}
        self.assertNotIn("fake", client.get_snapshot()["positions"])
        serialized = json.dumps(client.get_snapshot())
        self.assertNotIn("secret-api-key", serialized)
        self.assertNotIn("secret-passphrase", serialized)
        self.assertNotIn("secret-signature", serialized)

    def test_unknown_channel_does_not_mutate_private_state(self):
        client = self.make_client()
        before = client.get_snapshot()
        client._process_json_message({
            "action": "update",
            "arg": {"channel": "unknown"},
            "data": [{"positionId": "bad", "positions": "99"}],
        })
        after = client.get_snapshot()
        self.assertEqual(before["positions"], after["positions"])
        self.assertEqual(before["orders"], after["orders"])
        self.assertEqual(before["algo_orders"], after["algo_orders"])


if __name__ == "__main__":
    unittest.main()
