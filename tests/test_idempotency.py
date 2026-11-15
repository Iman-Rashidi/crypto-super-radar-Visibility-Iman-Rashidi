from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from crypto_super_radar.persistent_event_store import (
    IdempotencyViolationError,
    PersistentEventStore,
)


class PersistentEventStoreIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "events.sqlite3"
        self.store = PersistentEventStore(self.path)
        self.intent = self.store.register_or_fetch_intent(
            intent_id="intent-idem", client_order_id="client-idem",
            payload={"symbol": "BTC-USDT", "side": "BUY", "qty": 0.01},
            timestamp_utc="2026-11-15T10:00:00Z",
        )

    def tearDown(self):
        self.store.close()
        self.tempdir.cleanup()

    def transition(self, *, event_id="evt-validated", to_state="VALIDATED", **kwargs):
        values = {
            "intent_id": "intent-idem", "client_order_id": "client-idem",
            "payload_hash": self.intent["payload_hash"], "to_state": to_state,
            "event_type": "VALIDATION", "event_id": event_id,
            "timestamp_utc": "2026-11-15T10:00:01Z",
        }
        values.update(kwargs)
        return self.store.commit_transition_atomic(**values)

    def test_identical_event_retry_returns_original_without_new_sequence(self):
        first = self.transition()
        second = self.transition()
        self.assertTrue(first["inserted"])
        self.assertFalse(second["inserted"])
        self.assertEqual(first["event_hash"], second["event_hash"])
        self.assertEqual(len(self.store.list_events("intent-idem")), 2)

    def test_event_id_reuse_with_different_payload_is_rejected(self):
        self.transition()
        with self.assertRaises(IdempotencyViolationError):
            self.transition(to_state="SUBMITTING", event_type="SUBMIT")

    def test_transition_rejects_client_order_id_or_payload_hash_mismatch(self):
        with self.assertRaises(IdempotencyViolationError):
            self.transition(client_order_id="other-client")
        with self.assertRaises(IdempotencyViolationError):
            self.transition(payload_hash="f" * 64)

    def test_concurrent_identical_retries_create_exactly_one_event(self):
        second_store = PersistentEventStore(self.path)
        barrier = Barrier(2)

        def write(store):
            barrier.wait()
            return store.commit_transition_atomic(
                intent_id="intent-idem", client_order_id="client-idem",
                payload_hash=self.intent["payload_hash"], to_state="VALIDATED",
                event_type="VALIDATION", event_id="evt-concurrent",
                timestamp_utc="2026-11-15T10:00:01Z",
            )

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(write, (self.store, second_store)))
            self.assertEqual(sorted(item["inserted"] for item in results), [False, True])
            self.assertEqual(len(self.store.list_events("intent-idem")), 2)
            self.assertEqual(self.store.verify_event_chain("intent-idem")["status"], "PASS")
        finally:
            second_store.close()

    def test_concurrent_different_events_cannot_both_claim_same_transition(self):
        second_store = PersistentEventStore(self.path)
        barrier = Barrier(2)

        def write(store, event_id):
            barrier.wait()
            return store.commit_transition_atomic(
                intent_id="intent-idem", client_order_id="client-idem",
                payload_hash=self.intent["payload_hash"], to_state="VALIDATED",
                event_type="VALIDATION", event_id=event_id,
                timestamp_utc="2026-11-15T10:00:01Z",
            )

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(write, self.store, "evt-a"), pool.submit(write, second_store, "evt-b")]
                results = []
                errors = []
                for future in futures:
                    try:
                        results.append(future.result())
                    except ValueError as exc:
                        errors.append(exc)
            self.assertEqual(len(results), 1)
            self.assertEqual(len(errors), 1)
            self.assertEqual(len(self.store.list_events("intent-idem")), 2)
            self.assertEqual(self.store.verify_event_chain("intent-idem")["status"], "PASS")
        finally:
            second_store.close()


if __name__ == "__main__":
    unittest.main()
