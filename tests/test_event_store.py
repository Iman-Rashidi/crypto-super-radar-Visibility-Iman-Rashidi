from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from crypto_super_radar.persistent_event_store import (
    IdempotencyViolationError,
    PersistentEventStore,
)


class PersistentEventStoreRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "events.sqlite3"
        self.store = PersistentEventStore(self.path)
        self.payload = {"symbol": "BTC-USDT", "side": "BUY", "quantity": "0.01", "risk": {"mode": "ISOLATED"}}

    def tearDown(self):
        self.store.close()
        self.tempdir.cleanup()

    def register(self, **kwargs):
        values = {
            "intent_id": "intent-001",
            "client_order_id": "client-001",
            "payload": self.payload,
            "timestamp_utc": "2026-11-15T10:00:00.123Z",
        }
        values.update(kwargs)
        return self.store.register_or_fetch_intent(**values)

    def test_registration_is_atomic_and_creates_initial_event(self):
        result = self.register()
        events = self.store.list_events("intent-001")
        self.assertTrue(result["created"])
        self.assertEqual(result["current_state"], "INTENT_RECORDED")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["seq_num"], 1)
        self.assertEqual(events[0]["from_state"], "NONE")
        self.assertEqual(events[0]["to_state"], "INTENT_RECORDED")
        self.assertEqual(self.store.verify_event_chain("intent-001")["status"], "PASS")

    def test_exact_intent_replay_is_idempotent_and_canonical(self):
        first = self.register()
        replay = self.register(payload={"risk": {"mode": "ISOLATED"}, "quantity": "0.01", "side": "BUY", "symbol": "BTC-USDT"})
        self.assertTrue(first["created"])
        self.assertFalse(replay["created"])
        self.assertEqual(first["payload_hash"], replay["payload_hash"])
        self.assertEqual(len(self.store.list_events("intent-001")), 1)

    def test_existing_intent_rejects_changed_payload_or_client_id(self):
        self.register()
        with self.assertRaises(IdempotencyViolationError):
            self.register(payload={"symbol": "BTC-USDT", "side": "SELL", "quantity": "0.01"})
        with self.assertRaises(IdempotencyViolationError):
            self.register(client_order_id="client-changed")

    def test_client_order_id_cannot_be_reused_for_another_intent(self):
        self.register()
        with self.assertRaises(IdempotencyViolationError):
            self.register(intent_id="intent-002", payload={"symbol": "ETH-USDT"})

    def test_supplied_payload_hash_must_match_canonical_payload(self):
        with self.assertRaises(IdempotencyViolationError):
            self.register(payload_hash="0" * 64)

    def test_timestamps_are_normalized_to_utc_milliseconds(self):
        result = self.register(
            intent_id="intent-time", client_order_id="client-time",
            timestamp_utc="2026-11-15T11:00:00.987+01:00",
        )
        self.assertEqual(result["created_at_utc"], "2026-11-15T10:00:00.987Z")
        self.assertEqual(self.store.verify_event_chain("intent-time")["status"], "PASS")

    def test_naive_timestamp_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "timezone"):
            self.register(intent_id="intent-naive", client_order_id="client-naive", timestamp_utc="2026-11-15T10:00:00")

    def test_intent_and_initial_event_roll_back_together(self):
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                """CREATE TRIGGER reject_initial_event BEFORE INSERT ON trade_events
                   WHEN NEW.seq_num = 1 BEGIN SELECT RAISE(ABORT, 'forced initial event failure'); END"""
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(sqlite3.IntegrityError):
            self.register()

        connection = sqlite3.connect(self.path)
        try:
            count = connection.execute("SELECT COUNT(*) FROM trade_intents").fetchone()[0]
            self.assertEqual(count, 0)
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
