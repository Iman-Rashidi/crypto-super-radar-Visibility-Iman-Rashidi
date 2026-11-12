from __future__ import annotations

import sqlite3
import unittest

from crypto_super_radar.order_flow_store import OrderFlowEventStore


def event(trade_id="t-1", **overrides):
    value = {
        "venue": "binance",
        "symbol": "BTCUSDT",
        "trade_id": trade_id,
        "event_time_ms": 1_700_000_001_000,
        "received_at_ms": 1_700_000_001_010,
        "price": "65000.25",
        "base_quantity": "0.01",
        "aggressor_side": "BUY",
        "source": "binance_spot_aggTrade",
    }
    value.update(overrides)
    return value


class OrderFlowEventStoreTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.store = OrderFlowEventStore(self.connection)

    def tearDown(self):
        self.connection.close()

    def test_append_is_durable_across_store_instances_and_idempotent(self):
        self.assertTrue(self.store.append(event()))
        self.assertFalse(self.store.append(event()))
        second = OrderFlowEventStore(self.connection)
        rows = second.read_window(
            venue="binance", symbol="BTCUSDT",
            start_event_time_ms=1_700_000_000_000,
            end_event_time_ms=1_700_000_002_000,
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["trade_id"], "t-1")
        self.assertEqual(rows[0]["base_quantity"], 0.01)

    def test_batch_is_ordered_by_exchange_event_time(self):
        self.store.append_many([
            event("later", event_time_ms=1_700_000_002_000),
            event("earlier", event_time_ms=1_700_000_001_000),
        ])
        rows = self.store.read_window(
            venue="binance", symbol="BTCUSDT",
            start_event_time_ms=1_700_000_000_000,
            end_event_time_ms=1_700_000_003_000,
        )
        self.assertEqual([row["trade_id"] for row in rows], ["earlier", "later"])

    def test_same_native_id_on_different_venues_is_not_a_duplicate(self):
        self.store.append(event("same"))
        self.assertTrue(self.store.append(event("same", venue="bybit")))

    def test_invalid_batch_is_rejected_before_any_write(self):
        with self.assertRaisesRegex(ValueError, "base_quantity"):
            self.store.append_many([
                event("valid"),
                event("invalid", base_quantity=None),
            ])
        rows = self.store.read_window(
            venue="binance", symbol="BTCUSDT",
            start_event_time_ms=1_700_000_000_000,
            end_event_time_ms=1_700_000_003_000,
        )
        self.assertEqual(rows, [])

    def test_venue_symbol_scope_and_limit_are_enforced(self):
        self.store.append_many([
            event("one"),
            event("two", event_time_ms=1_700_000_002_000),
            event("eth", symbol="ETHUSDT"),
            event("other-venue", venue="bybit"),
        ])
        rows = self.store.read_window(
            venue="binance", symbol="BTCUSDT",
            start_event_time_ms=1_700_000_000_000,
            end_event_time_ms=1_700_000_003_000,
            limit=1,
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["trade_id"], "one")


if __name__ == "__main__":
    unittest.main()
