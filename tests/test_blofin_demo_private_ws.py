from __future__ import annotations

import asyncio
import json
import sqlite3
import unittest

from crypto_super_radar.blofin_demo_readonly import BloFinDemoReadOnly
from crypto_super_radar.blofin_demo_private_ws import (
    DEFAULT_PRIVATE_SYMBOLS,
    PRIVATE_CHANNELS,
    BloFinDemoPrivateEventStore,
    BloFinDemoPrivateWSError,
    BloFinDemoPrivateWebSocketWorker,
    build_private_subscription,
    normalize_blofin_private_futures_message,
)


def read_only_client():
    return BloFinDemoReadOnly(
        api_key="demo-test-key",
        api_secret="demo-test-secret",
        passphrase="demo-test-passphrase",
        clock_ms=lambda: 1_800_000_000_000,
    )


class BloFinDemoPrivateSubscriptionTests(unittest.TestCase):
    def test_subscription_is_fixed_to_demo_read_only_private_channels(self):
        result = build_private_subscription(("BTC-USDT", "ETH-USDT"))
        self.assertEqual(result["op"], "subscribe")
        self.assertEqual(len(result["args"]), 6)
        self.assertEqual(
            {(item["channel"], item["instId"]) for item in result["args"]},
            {(channel, symbol) for channel in PRIVATE_CHANNELS for symbol in ("BTC-USDT", "ETH-USDT")},
        )
        with self.assertRaises(ValueError):
            build_private_subscription(("BTC-USDT", "BTC-USDT"))
        with self.assertRaises(ValueError):
            build_private_subscription(("BTC-USDT", "BTC/USDT"))

    def test_subscription_does_not_include_order_write_or_account_balance_channels(self):
        encoded = json.dumps(build_private_subscription(("BTC-USDT",)))
        self.assertNotIn("place-order", encoded)
        self.assertNotIn("cancel-order", encoded)
        self.assertNotIn('"channel":"account"', encoded)
        self.assertNotIn('"op":"login"', encoded)


class BloFinDemoPrivateMessageTests(unittest.TestCase):
    def setUp(self):
        self.received = 1_800_000_000_100
        self.symbols = ("BTC-USDT",)

    def norm(self, channel, row, action="update"):
        return normalize_blofin_private_futures_message(
            {
                "action": action,
                "arg": {"channel": channel, "instId": "BTC-USDT"},
                "data": row if isinstance(row, list) else [row],
            },
            expected_symbols=self.symbols,
            received_at_ms=self.received,
        )

    def test_login_and_subscription_acknowledgements_are_control_only(self):
        login = normalize_blofin_private_futures_message(
            {"event": "login", "code": "0", "msg": ""},
            expected_symbols=self.symbols,
            received_at_ms=self.received,
        )
        ack = normalize_blofin_private_futures_message(
            {"event": "subscribe", "arg": {"channel": "orders", "instId": "BTC-USDT"}},
            expected_symbols=self.symbols,
            received_at_ms=self.received,
        )
        self.assertEqual(login["type"], "control")
        self.assertEqual(ack["channel"], "orders")
        self.assertEqual(ack["inst_id"], "BTC-USDT")
        self.assertEqual(login["events"], [])

    def test_positions_snapshot_preserves_exact_position_id_and_size(self):
        event = self.norm("positions", {
            "instType": "SWAP", "instId": "BTC-USDT", "marginMode": "isolated",
            "positionId": "p-1", "positionSide": "long", "positions": "0.01",
            "availablePositions": "0.01", "averagePrice": "65000",
            "unrealizedPnl": "2.1", "markPrice": "65210", "leverage": "2",
            "updateTime": "1800000000050",
        }, action="snapshot")["events"][0]
        self.assertEqual(event["payload"]["position_id"], "p-1")
        self.assertEqual(event["payload"]["positions"], "0.01")
        self.assertEqual(event["action"], "snapshot")

    def test_empty_positions_snapshot_is_a_valid_read_only_state_observation(self):
        result = normalize_blofin_private_futures_message(
            {"arg": {"channel": "positions", "instId": "BTC-USDT"}, "data": []},
            expected_symbols=self.symbols,
            received_at_ms=self.received,
        )
        self.assertEqual(result["events"][0]["payload"]["items"], [])
        self.assertTrue(result["events"][0]["payload"]["empty_snapshot"])

    def test_order_update_is_normalized_but_never_treated_as_initial_order_snapshot(self):
        event = self.norm("orders", {
            "instType": "SWAP", "instId": "BTC-USDT", "orderId": "o-1",
            "clientOrderId": "csr-1", "state": "filled", "size": "10",
            "filledSize": "10", "side": "buy", "positionSide": "long",
            "marginMode": "isolated", "tpTriggerPrice": "66000",
            "slTriggerPrice": "64000", "updateTime": "1800000000050",
        })["events"][0]
        self.assertEqual(event["payload"]["order_id"], "o-1")
        self.assertEqual(event["payload"]["position_id"], "")
        self.assertEqual(event["payload"]["tp_trigger_price"], "66000")

    def test_algo_update_preserves_tpsl_identifiers_and_trigger_types(self):
        event = self.norm("orders-algo", {
            "instType": "SWAP", "instId": "BTC-USDT", "tpslId": "t-1",
            "algoId": "a-1", "state": "live", "size": "10",
            "positionSide": "long", "marginMode": "isolated",
            "tpTriggerPrice": "66000", "tpTriggerPriceType": "mark",
            "slTriggerPrice": "64000", "slTriggerPriceType": "mark",
            "updateTime": "1800000000050",
        })["events"][0]
        self.assertEqual(event["payload"]["tpsl_id"], "t-1")
        self.assertEqual(event["payload"]["tp_trigger_type"], "mark")
        self.assertEqual(event["payload"]["sl_trigger_type"], "mark")

    def test_unknown_private_channel_and_wrong_instrument_fail_closed(self):
        with self.assertRaisesRegex(BloFinDemoPrivateWSError, "CHANNEL_NOT_ALLOWLISTED"):
            normalize_blofin_private_futures_message(
                {"arg": {"channel": "account"}, "data": []},
                expected_symbols=self.symbols,
                received_at_ms=self.received,
            )
        with self.assertRaisesRegex(BloFinDemoPrivateWSError, "UNEXPECTED_INSTRUMENT"):
            self.norm("orders", {
                "instType": "SWAP", "instId": "ETH-USDT", "orderId": "o-2",
                "state": "live", "updateTime": "1800000000050",
            })


class BloFinDemoPrivateEventStoreTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.store = BloFinDemoPrivateEventStore(self.db)
        self.event = normalize_blofin_private_futures_message(
            {
                "action": "snapshot",
                "arg": {"channel": "positions", "instId": "BTC-USDT"},
                "data": [{
                    "instType": "SWAP", "instId": "BTC-USDT", "marginMode": "isolated",
                    "positionId": "p-1", "positionSide": "long", "positions": "0.01",
                    "availablePositions": "0.01", "updateTime": "1800000000050",
                }],
            },
            expected_symbols=("BTC-USDT",),
            received_at_ms=1_800_000_000_100,
        )["events"][0]

    def tearDown(self):
        self.db.close()

    def test_store_is_idempotent_and_append_only(self):
        self.assertEqual(self.store.append_many([self.event]), {"inserted": 1, "duplicates": 0})
        self.assertEqual(self.store.append_many([self.event]), {"inserted": 0, "duplicates": 1})
        rows = self.store.read_recent(inst_id="BTC-USDT", channel="positions")
        self.assertEqual(rows[0]["payload"]["position_id"], "p-1")
        with self.assertRaises(sqlite3.DatabaseError):
            self.db.execute("UPDATE blofin_demo_private_events SET action='update'")
        with self.assertRaises(sqlite3.DatabaseError):
            self.db.execute("DELETE FROM blofin_demo_private_events")


class BloFinDemoPrivateWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_logs_in_subscribes_and_persists_without_trade_authority(self):
        db = sqlite3.connect(":memory:")
        store = BloFinDemoPrivateEventStore(db)
        stop = asyncio.Event()
        client = read_only_client()

        async def connector():
            yield {"event": "login", "code": "0"}
            for symbol in ("BTC-USDT",):
                for channel in PRIVATE_CHANNELS:
                    yield {"event": "subscribe", "arg": {"channel": channel, "instId": symbol}}
            yield {
                "action": "snapshot",
                "arg": {"channel": "positions", "instId": "BTC-USDT"},
                "data": [{
                    "instType": "SWAP", "instId": "BTC-USDT", "marginMode": "isolated",
                    "positionId": "p-1", "positionSide": "long", "positions": "0.01",
                    "availablePositions": "0.01", "updateTime": "1800000000050",
                }],
            }
            yield {
                "action": "update",
                "arg": {"channel": "orders", "instId": "BTC-USDT"},
                "data": [{
                    "instType": "SWAP", "instId": "BTC-USDT", "orderId": "o-1",
                    "state": "filled", "size": "10", "filledSize": "10",
                    "updateTime": "1800000000060",
                }],
            }
            stop.set()

        worker = BloFinDemoPrivateWebSocketWorker(
            client=client, event_store=store, inst_ids=("BTC-USDT",),
            connector=connector, clock_ms=lambda: 1_800_000_000_100, max_attempts=1,
        )
        report = await worker.run(stop)
        self.assertTrue(report["login_authenticated"])
        self.assertTrue(report["all_private_subscriptions_acknowledged"])
        self.assertEqual(report["positions_first_observation_symbols"], ["BTC-USDT"])
        self.assertTrue(report["initial_order_state_requires_rest_reconciliation"])
        self.assertTrue(report["initial_tpsl_state_requires_rest_reconciliation"])
        self.assertEqual(report["channel_event_counts"], {"positions": 1, "orders": 1})
        self.assertEqual(report["trade_authorization"], "NO_TRADE")
        self.assertFalse(report["orders_submitted"])
        self.assertFalse(report["orders_cancelled"])
        self.assertFalse(report["positions_closed"])
        rows = store.read_recent(inst_id="BTC-USDT", channel="positions")
        self.assertEqual(rows[0]["payload"]["position_id"], "p-1")
        db.close()

    async def test_worker_rejects_subscription_ack_before_login(self):
        db = sqlite3.connect(":memory:")
        store = BloFinDemoPrivateEventStore(db)
        stop = asyncio.Event()

        async def connector():
            yield {"event": "subscribe", "arg": {"channel": "positions", "instId": "BTC-USDT"}}
            stop.set()

        worker = BloFinDemoPrivateWebSocketWorker(
            client=read_only_client(), event_store=store,
            inst_ids=("BTC-USDT",), connector=connector, max_attempts=1,
        )
        report = await worker.run(stop)
        self.assertIn("BloFinDemoPrivateWSError", report["last_error"])
        self.assertFalse(report["login_authenticated"])
        self.assertEqual(report["subscription_ack_count"], 0)
        db.close()


if __name__ == "__main__":
    unittest.main()
