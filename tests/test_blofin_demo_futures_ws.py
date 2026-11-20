from __future__ import annotations

import asyncio
import json
import sqlite3
import unittest

from crypto_super_radar.blofin_demo_futures_ws import (
    BloFinDemoFuturesPublicEventStore,
    BloFinDemoFuturesPublicWorker,
    BloFinDemoWebSocketError,
    build_public_subscription,
    normalize_blofin_demo_futures_message,
)


class BloFinDemoFuturesNormalizationTests(unittest.TestCase):
    def setUp(self):
        self.inst = "BTC-USDT"
        self.received = 1_800_000_000_100
        self.contract_value = 0.01

    def normalize(self, channel, data):
        return normalize_blofin_demo_futures_message(
            {"arg": {"channel": channel, "instId": self.inst}, "data": data},
            inst_id=self.inst,
            received_at_ms=self.received,
            contract_value=self.contract_value,
        )

    def test_subscription_is_demo_public_channels_only(self):
        sub = build_public_subscription(self.inst)
        self.assertEqual(sub["op"], "subscribe")
        self.assertEqual(
            [arg["channel"] for arg in sub["args"]],
            ["trades", "books5", "tickers", "funding-rate", "mark-price-candle1m"],
        )
        self.assertTrue(all(arg["instId"] == self.inst for arg in sub["args"]))
        with self.assertRaises(ValueError):
            build_public_subscription(self.inst, ("orders",))
        with self.assertRaises(ValueError):
            build_public_subscription(self.inst, ("positions",))
        with self.assertRaises(ValueError):
            build_public_subscription(self.inst, ("trades", "trades"))

    def test_trade_contract_size_is_converted_only_with_explicit_contract_value(self):
        result = self.normalize("trades", [{
            "instId": self.inst, "tradeId": "trade-1", "price": "65000",
            "size": "12", "side": "buy", "ts": "1800000000050",
        }])
        event = result["events"][0]
        self.assertEqual(event["payload"]["contract_size"], 12.0)
        self.assertEqual(event["payload"]["contract_value"], 0.01)
        self.assertAlmostEqual(event["payload"]["base_quantity"], 0.12)
        self.assertEqual(event["payload"]["aggressor_side"], "BUY")
        self.assertEqual(event["event_time_ms"], 1_800_000_000_050)

    def test_trade_rejects_wrong_instrument_or_missing_side(self):
        bad_symbol = {"arg": {"channel": "trades", "instId": "ETH-USDT"},
                      "data": [{"instId": "ETH-USDT", "tradeId": "x", "price": "1",
                                "size": "1", "side": "buy", "ts": str(self.received)}]}
        with self.assertRaisesRegex(BloFinDemoWebSocketError, "INSTRUMENT_MISMATCH"):
            normalize_blofin_demo_futures_message(
                bad_symbol, inst_id=self.inst, received_at_ms=self.received,
                contract_value=self.contract_value,
            )
        with self.assertRaisesRegex(BloFinDemoWebSocketError, "INVALID_TRADE_SIDE"):
            self.normalize("trades", [{
                "instId": self.inst, "tradeId": "trade-2", "price": "65000",
                "size": "1", "side": "hold", "ts": str(self.received),
            }])

    def test_books5_requires_a_valid_uncrossed_complete_snapshot(self):
        event = self.normalize("books5", {
            "asks": [["65002", "3"], ["65003", "1"]],
            "bids": [["65001", "2"], ["65000", "2"]],
            "ts": str(self.received),
        })["events"][0]
        self.assertEqual(event["payload"]["asks"][0], [65002.0, 3.0])
        self.assertEqual(event["payload"]["bids"][0], [65001.0, 2.0])
        with self.assertRaisesRegex(BloFinDemoWebSocketError, "CROSSED_BLOFIN_BOOK"):
            self.normalize("books5", {
                "asks": [["64999", "3"]], "bids": [["65001", "2"]],
                "ts": str(self.received),
            })

    def test_ticker_checks_exchange_timestamp_and_spread(self):
        event = self.normalize("tickers", {
            "instId": self.inst, "last": "65000", "lastSize": "1",
            "askPrice": "65001", "askSize": "2",
            "bidPrice": "64999", "bidSize": "3", "ts": str(self.received),
        })["events"][0]
        self.assertEqual(event["payload"]["last"], 65000.0)
        with self.assertRaisesRegex(BloFinDemoWebSocketError, "CROSSED_BLOFIN_TICKER"):
            self.normalize("tickers", {
                "instId": self.inst, "last": "65000", "lastSize": "1",
                "askPrice": "64998", "askSize": "2",
                "bidPrice": "64999", "bidSize": "3", "ts": str(self.received),
            })

    def test_funding_time_is_upcoming_settlement_not_observation_time(self):
        event = self.normalize("funding-rate", [{
            "instId": self.inst, "fundingRate": "-0.0001",
            "fundingTime": "1800003600000",
        }])["events"][0]
        self.assertIsNone(event["event_time_ms"])
        self.assertEqual(event["payload"]["upcoming_settlement_time_ms"], 1_800_003_600_000)
        self.assertEqual(event["payload"]["timestamp_semantics"], "UPCOMING_SETTLEMENT_NOT_EVENT_TIME")
        self.assertEqual(event["payload"]["funding_rate"], -0.0001)

    def test_mark_price_candle_preserves_unconfirmed_and_closed_state(self):
        forming, closed = self.normalize("mark-price-candle1m", [
            [str(self.received), "65000", "65020", "64990", "65010", "0"],
            [str(self.received + 60_000), "65010", "65030", "65000", "65025", "1"],
        ])["events"]
        self.assertFalse(forming["payload"]["confirmed"])
        self.assertTrue(closed["payload"]["confirmed"])


class BloFinDemoPublicEventStoreTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.store = BloFinDemoFuturesPublicEventStore(self.db)
        self.event = {
            "event_id": "event-1", "inst_id": "BTC-USDT", "channel": "trades",
            "event_time_ms": 1_800_000_000_000, "received_at_ms": 1_800_000_000_010,
            "payload": {"trade_id": "1", "base_quantity": 0.01},
            "source": "blofin_demo_futures_public_trades",
        }

    def tearDown(self):
        self.db.close()

    def test_store_is_idempotent_and_reads_back_exact_event(self):
        self.assertEqual(self.store.append_many([self.event]), {"inserted": 1, "duplicates": 0})
        self.assertEqual(self.store.append_many([self.event]), {"inserted": 0, "duplicates": 1})
        rows = self.store.read_recent(inst_id="BTC-USDT", channel="trades")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["payload"]["trade_id"], "1")

    def test_store_is_append_only(self):
        self.store.append_many([self.event])
        with self.assertRaises(sqlite3.DatabaseError):
            self.db.execute("UPDATE blofin_demo_futures_public_events SET source='changed'")
        with self.assertRaises(sqlite3.DatabaseError):
            self.db.execute("DELETE FROM blofin_demo_futures_public_events")


class BloFinDemoPublicWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_persists_only_normalized_public_demo_events(self):
        db = sqlite3.connect(":memory:")
        store = BloFinDemoFuturesPublicEventStore(db)
        stop = asyncio.Event()

        async def connector():
            yield json.dumps({"event": "subscribe", "arg": {"channel": "trades", "instId": "BTC-USDT"}})
            yield json.dumps({"arg": {"channel": "trades", "instId": "BTC-USDT"},
                              "data": [{"instId": "BTC-USDT", "tradeId": "w1",
                                        "price": "65000", "size": "10", "side": "buy",
                                        "ts": "1800000000001"}]})
            yield json.dumps({"arg": {"channel": "funding-rate", "instId": "BTC-USDT"},
                              "data": [{"instId": "BTC-USDT", "fundingRate": "-0.0001",
                                        "fundingTime": "1800003600000"}]})
            stop.set()

        worker = BloFinDemoFuturesPublicWorker(
            inst_id="BTC-USDT", contract_value=0.01, event_store=store,
            connector=connector, clock_ms=lambda: 1_800_000_000_100, max_attempts=1,
        )
        result = await worker.run(stop)
        self.assertEqual(result["status"], "STOPPED")
        self.assertEqual(result["environment"], "BLOFIN_DEMO_PUBLIC_WS")
        self.assertEqual(result["authentication"], "NOT_USED_PUBLIC_CHANNELS_ONLY")
        self.assertEqual(result["trade_authorization"], "NO_TRADE")
        self.assertFalse(result["orders_submitted"])
        self.assertEqual(result["observations"], 2)
        self.assertEqual(result["channel_observation_counts"], {"trades": 1, "funding-rate": 1})
        rows = store.read_recent(inst_id="BTC-USDT", channel="trades")
        self.assertEqual(rows[0]["payload"]["base_quantity"], 0.1)
        db.close()

    async def test_worker_fails_closed_when_persistence_is_not_confirmed(self):
        class BadStore:
            def append_many(self, events):
                return {"inserted": 0, "duplicates": 0}

        stop = asyncio.Event()

        async def connector():
            yield {"arg": {"channel": "trades", "instId": "BTC-USDT"},
                   "data": [{"instId": "BTC-USDT", "tradeId": "w2", "price": "10",
                             "size": "2", "side": "sell", "ts": "1800000000001"}]}
            stop.set()

        worker = BloFinDemoFuturesPublicWorker(
            inst_id="BTC-USDT", contract_value=0.01, event_store=BadStore(),
            connector=connector, clock_ms=lambda: 1_800_000_000_100, max_attempts=1,
        )
        result = await worker.run(stop)
        self.assertIn("PUBLIC_EVENT_PERSISTENCE_NOT_CONFIRMED", result["last_error"])
        self.assertEqual(result["observations"], 0)


if __name__ == "__main__":
    unittest.main()
