from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import unittest

from crypto_super_radar.challenge1_event_queue import (
    Challenge1EventQueue,
    Challenge1QueueError,
    QueueIdempotencyConflict,
)


def sandbox_event(event_id: str = "sandbox-replay-0001", payload=None):
    return {
        "event_id": event_id,
        "producer": "AI_SANDBOX",
        "event_type": "REPLAY_REPORT",
        "occurred_at_ms": 1_800_000_000_000,
        "payload": payload or {"report_id": "report-001", "oos_status": "UNKNOWN"},
    }


def demo_event(event_id: str = "demo-position-0001"):
    return {
        "event_id": event_id,
        "producer": "BLOFIN_DEMO_ENGINE",
        "event_type": "DEMO_POSITION_OUTCOME",
        "occurred_at_ms": 1_800_000_000_000,
        "payload": {"position_id": "position-123", "realized_net_pnl_usdt": 1.25},
    }


class Challenge1EventQueueTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:", check_same_thread=False, timeout=10)
        self.connection.row_factory = sqlite3.Row
        self.queue = Challenge1EventQueue(self.connection)

    def tearDown(self):
        self.connection.close()

    def test_sandbox_events_are_routed_only_to_read_only_demo_audit(self):
        self.assertTrue(self.queue.enqueue(sandbox_event(), now_ms=1000))
        claimed = self.queue.claim_next(
            "DEMO_AUDIT", worker_id="demo-audit-1", now_ms=1000, lease_ms=5000
        )
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["consumer"], "DEMO_AUDIT")
        self.assertEqual(claimed["producer"], "AI_SANDBOX")
        self.assertFalse(self.queue.acknowledge(
            claimed["event_id"], "SANDBOX", worker_id="demo-audit-1", now_ms=1001
        ))

    def test_demo_evidence_is_routed_to_sandbox_not_to_order_execution(self):
        self.assertTrue(self.queue.enqueue(demo_event(), now_ms=1000))
        self.assertIsNone(self.queue.claim_next(
            "DEMO_AUDIT", worker_id="audit-1", now_ms=1000
        ))
        claimed = self.queue.claim_next("SANDBOX", worker_id="sandbox-1", now_ms=1000)
        self.assertEqual(claimed["event_type"], "DEMO_POSITION_OUTCOME")
        self.assertTrue(self.queue.acknowledge(
            claimed["event_id"], "SANDBOX", worker_id="sandbox-1", now_ms=1001
        ))

    def test_duplicate_is_idempotent_and_conflicting_payload_is_rejected(self):
        event = sandbox_event()
        self.assertTrue(self.queue.enqueue(event, now_ms=1000))
        self.assertFalse(self.queue.enqueue(event, now_ms=1001))
        changed = sandbox_event(payload={"report_id": "other-report"})
        with self.assertRaises(QueueIdempotencyConflict):
            self.queue.enqueue(changed, now_ms=1002)

    def test_unknown_event_type_and_order_commands_are_rejected(self):
        event = sandbox_event()
        event["event_type"] = "ORDER_CREATE"
        with self.assertRaisesRegex(Challenge1QueueError, "producer/event_type"):
            self.queue.enqueue(event, now_ms=1000)
        event = sandbox_event(payload={"nested": {"command": "CLOSE_ALL"}})
        with self.assertRaisesRegex(Challenge1QueueError, "prohibited command"):
            self.queue.enqueue(event, now_ms=1000)

    def test_claim_is_exclusive_until_acknowledged(self):
        self.queue.enqueue(sandbox_event(), now_ms=1000)
        claimed = self.queue.claim_next(
            "DEMO_AUDIT", worker_id="worker-a", now_ms=1000, lease_ms=5000
        )
        self.assertIsNotNone(claimed)
        self.assertIsNone(self.queue.claim_next(
            "DEMO_AUDIT", worker_id="worker-b", now_ms=1001, lease_ms=5000
        ))
        self.assertTrue(self.queue.acknowledge(
            claimed["event_id"], "DEMO_AUDIT", worker_id="worker-a", now_ms=1002
        ))
        self.assertIsNone(self.queue.claim_next(
            "DEMO_AUDIT", worker_id="worker-b", now_ms=1003, lease_ms=5000
        ))
        self.assertEqual(
            self.queue.delivery_status(claimed["event_id"], "DEMO_AUDIT")["status"],
            "ACKNOWLEDGED",
        )

    def test_expired_lease_is_recovered_after_worker_restart(self):
        self.queue.enqueue(sandbox_event(), now_ms=1000)
        first = self.queue.claim_next(
            "DEMO_AUDIT", worker_id="worker-before-restart", now_ms=1000, lease_ms=1000
        )
        self.assertEqual(first["attempt"], 1)
        recovered = self.queue.claim_next(
            "DEMO_AUDIT", worker_id="worker-after-restart", now_ms=2001, lease_ms=1000
        )
        self.assertEqual(recovered["event_id"], first["event_id"])
        self.assertEqual(recovered["attempt"], 2)
        states = [row["state"] for row in self.queue.history(first["event_id"])]
        self.assertEqual(states, ["PENDING", "PROCESSING", "PROCESSING"])

    def test_reject_schedules_retry_then_dead_letters_after_attempt_limit(self):
        self.queue.enqueue(sandbox_event(), now_ms=1000)
        first = self.queue.claim_next(
            "DEMO_AUDIT", worker_id="worker-1", now_ms=1000, lease_ms=1000
        )
        state = self.queue.reject(
            first["event_id"], "DEMO_AUDIT", worker_id="worker-1",
            error_type="TransientNetworkError", retry_delay_ms=1000,
            max_attempts=2, now_ms=1001,
        )
        self.assertEqual(state, "RETRY")
        self.assertIsNone(self.queue.claim_next(
            "DEMO_AUDIT", worker_id="worker-2", now_ms=1500, lease_ms=1000
        ))
        second = self.queue.claim_next(
            "DEMO_AUDIT", worker_id="worker-2", now_ms=2001, lease_ms=1000
        )
        state = self.queue.reject(
            second["event_id"], "DEMO_AUDIT", worker_id="worker-2",
            error_type="PermanentFailure", retry_delay_ms=1000,
            max_attempts=2, now_ms=2002,
        )
        self.assertEqual(state, "DEAD_LETTER")
        status = self.queue.delivery_status(second["event_id"], "DEMO_AUDIT")
        self.assertEqual(status["status"], "DEAD_LETTER")
        self.assertEqual(status["last_error_type"], "PermanentFailure")

    def test_source_event_and_delivery_history_are_append_only(self):
        self.queue.enqueue(sandbox_event(), now_ms=1000)
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "UPDATE csr_challenge1_queue_events SET event_type='EDITED'"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("DELETE FROM csr_challenge1_queue_events")
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "UPDATE csr_challenge1_queue_history SET state='EDITED'"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("DELETE FROM csr_challenge1_queue_history")

    def test_queue_claim_is_atomic_across_independent_sqlite_connections(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "queue.sqlite3")
            setup = sqlite3.connect(path, timeout=10)
            setup.row_factory = sqlite3.Row
            queue = Challenge1EventQueue(setup)
            queue.enqueue(sandbox_event(), now_ms=1000)
            setup.close()

            barrier = threading.Barrier(2)
            results = []
            errors = []

            def claim(worker):
                connection = sqlite3.connect(path, timeout=10)
                connection.row_factory = sqlite3.Row
                local_queue = Challenge1EventQueue(connection)
                try:
                    barrier.wait(timeout=5)
                    result = local_queue.claim_next(
                        "DEMO_AUDIT", worker_id=worker, now_ms=1000, lease_ms=5000
                    )
                    results.append(result)
                except Exception as exc:
                    errors.append(type(exc).__name__)
                finally:
                    connection.close()

            threads = [
                threading.Thread(target=claim, args=("worker-1",)),
                threading.Thread(target=claim, args=("worker-2",)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

            self.assertFalse(errors)
            self.assertEqual(sum(item is not None for item in results), 1)


if __name__ == "__main__":
    unittest.main()
