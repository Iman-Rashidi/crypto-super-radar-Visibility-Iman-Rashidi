from __future__ import annotations

import asyncio
import json
import sqlite3
import unittest

from crypto_super_radar.order_flow import VenueOrderFlow
from crypto_super_radar.order_flow_runtime import OrderFlowStreamWorker
from crypto_super_radar.order_flow_store import OrderFlowEventStore


class OrderFlowRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_connects_normalizes_and_persists_public_binance_trade(self):
        db = sqlite3.connect(":memory:")
        store = OrderFlowEventStore(db)
        tracker = VenueOrderFlow("binance", window_ms=1_000, min_coverage_pct=10, stale_after_ms=10_000)
        stop = asyncio.Event()
        clock_values = iter([
            1_700_000_000_000, 1_700_000_000_020,
            1_700_000_000_030, 1_700_000_000_040,
        ])

        async def connector():
            yield json.dumps({
                "e": "aggTrade", "s": "BTCUSDT", "a": 77,
                "p": "65000", "q": "0.01", "T": 1_700_000_000_010, "m": False,
            })
            stop.set()

        worker = OrderFlowStreamWorker(
            provider="binance", symbol="BTCUSDT", tracker=tracker, event_store=store,
            connector=connector, clock_ms=lambda: next(clock_values), max_attempts=1,
        )
        result = await worker.run(stop)
        rows = store.read_window(
            venue="binance", symbol="BTCUSDT",
            start_event_time_ms=1_700_000_000_000,
            end_event_time_ms=1_700_000_001_000,
        )
        self.assertEqual(result["status"], "STOPPED")
        self.assertEqual(result["accepted"], 1)
        self.assertEqual(result["persisted"], 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["trade_id"], "77")
        self.assertEqual(rows[0]["aggressor_side"], "BUY")
        self.assertEqual(result["feed_status"], "DISCONNECTED")
        self.assertEqual(result["trade_authorization"], "NO_TRADE")
        db.close()

    async def test_worker_reconnects_after_stream_ends_and_uses_backoff(self):
        db = sqlite3.connect(":memory:")
        store = OrderFlowEventStore(db)
        tracker = VenueOrderFlow("binance", window_ms=1_000)
        stop = asyncio.Event()
        calls = {"connector": 0, "sleep": []}

        async def connector():
            calls["connector"] += 1
            if calls["connector"] == 2:
                stop.set()
            if False:
                yield None

        async def fake_sleep(seconds):
            calls["sleep"].append(seconds)

        worker = OrderFlowStreamWorker(
            provider="binance", symbol="BTCUSDT", tracker=tracker, event_store=store,
            connector=connector, clock_ms=lambda: 1_700_000_000_000,
            sleep=fake_sleep, base_backoff_seconds=0.25, max_backoff_seconds=1,
            max_attempts=2,
        )
        result = await worker.run(stop)
        self.assertEqual(calls["connector"], 2)
        self.assertEqual(calls["sleep"], [0.25])
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(result["status"], "STOPPED")
        db.close()

    async def test_invalid_json_freezes_and_does_not_persist(self):
        db = sqlite3.connect(":memory:")
        store = OrderFlowEventStore(db)
        tracker = VenueOrderFlow("binance", window_ms=1_000)
        stop = asyncio.Event()

        async def connector():
            yield "{bad-json"
            stop.set()

        worker = OrderFlowStreamWorker(
            provider="binance", symbol="BTCUSDT", tracker=tracker, event_store=store,
            connector=connector, clock_ms=lambda: 1_700_000_000_000, max_attempts=1,
        )
        result = await worker.run(stop)
        rows = store.read_window(
            venue="binance", symbol="BTCUSDT",
            start_event_time_ms=1_700_000_000_000,
            end_event_time_ms=1_700_000_001_000,
        )
        self.assertEqual(result["rejected"], 1)
        self.assertEqual(rows, [])
        db.close()


if __name__ == "__main__":
    unittest.main()
