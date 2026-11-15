from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from crypto_super_radar.order_state_machine import OrderStateMachine, TransitionValidationError
from crypto_super_radar.persistent_event_store import PersistentEventStore


class PersistentEventStoreTransitionTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "transitions.sqlite3"
        self.store = PersistentEventStore(self.path)
        self.intent = self.store.register_or_fetch_intent(
            intent_id="intent-state", client_order_id="client-state",
            payload={"symbol": "BTC-USDT", "side": "BUY", "quantity": 0.1},
            timestamp_utc="2026-11-15T12:00:00Z",
        )

    def tearDown(self):
        self.store.close()
        self.tempdir.cleanup()

    def transition(self, event_id, to_state, *, event_type="STATE_TRANSITION", evidence=None):
        return self.store.commit_transition_atomic(
            intent_id="intent-state", client_order_id="client-state",
            payload_hash=self.intent["payload_hash"], to_state=to_state,
            event_type=event_type, event_id=event_id, evidence=evidence,
        )

    def test_state_machine_exposes_allow_list_and_terminal_states(self):
        self.assertIn("VALIDATED", OrderStateMachine.allowed_next("INTENT_RECORDED"))
        self.assertEqual(OrderStateMachine.allowed_next("CLOSED"), ())
        self.assertNotIn("SUBMITTING", OrderStateMachine.allowed_next("UNKNOWN"))

    def test_invalid_transition_is_rejected(self):
        with self.assertRaises(TransitionValidationError):
            self.transition("skip-validation", "SUBMITTED")
        self.assertEqual(self.store.get_intent("intent-state")["current_state"], "INTENT_RECORDED")
        self.assertEqual(len(self.store.list_events("intent-state")), 1)

    def test_acknowledgement_requires_exchange_order_id(self):
        self.transition("ack-validated", "VALIDATED")
        self.transition("ack-submitting", "SUBMITTING")
        self.transition("ack-submitted", "SUBMITTED")
        with self.assertRaises(TransitionValidationError):
            self.transition("ack-no-id", "ACKNOWLEDGED")
        result = self.store.commit_transition_atomic(
            intent_id="intent-state", client_order_id="client-state",
            payload_hash=self.intent["payload_hash"], to_state="ACKNOWLEDGED",
            event_type="ACK", event_id="ack-with-id", exchange_order_id="exchange-ack-1",
        )
        self.assertEqual(result["exchange_order_id"], "exchange-ack-1")

    def test_unknown_cannot_retry_and_requires_verified_reconciliation(self):
        self.transition("unknown-event", "UNKNOWN", event_type="REQUEST_OUTCOME_UNKNOWN")
        with self.assertRaises(TransitionValidationError):
            self.transition("retry-after-unknown", "SUBMITTING", event_type="RETRY")
        with self.assertRaises(TransitionValidationError):
            self.transition("no-evidence", "ACKNOWLEDGED", event_type="RECONCILIATION")

        evidence = {
            "kind": "RECONCILIATION", "verified": True,
            "source": "BLOFIN_DEMO_READ_ONLY", "reference_id": "read-0001",
            "observed_at_utc": "2026-11-15T12:01:00Z", "exchange_status": "OPEN",
            "exchange_order_id": "exchange-001",
        }
        with self.assertRaises(TransitionValidationError):
            self.store.commit_transition_atomic(
                intent_id="intent-state", client_order_id="client-state",
                payload_hash=self.intent["payload_hash"], to_state="ACKNOWLEDGED",
                event_type="RECONCILIATION", event_id="mismatched-reconciliation",
                exchange_order_id="exchange-wrong", evidence=evidence,
            )
        result = self.store.commit_transition_atomic(
            intent_id="intent-state", client_order_id="client-state",
            payload_hash=self.intent["payload_hash"], to_state="ACKNOWLEDGED",
            event_type="RECONCILIATION", event_id="verified-reconciliation",
            exchange_order_id="exchange-001", evidence=evidence,
        )
        self.assertEqual(result["to_state"], "ACKNOWLEDGED")
        self.assertEqual(self.store.verify_event_chain("intent-state")["status"], "PASS")

    def _open_unprotected_position(self):
        sequence = ["VALIDATED", "SUBMITTING", "SUBMITTED", "ACKNOWLEDGED", "FILLED", "POSITION_OPEN_UNPROTECTED"]
        for index, state in enumerate(sequence, 1):
            details = {"exchange_order_id": "exchange-position-123"} if state == "ACKNOWLEDGED" else {}
            self.store.commit_transition_atomic(
                intent_id="intent-state", client_order_id="client-state",
                payload_hash=self.intent["payload_hash"], to_state=state,
                event_type="STATE_TRANSITION", event_id=f"position-step-{index}", **details,
            )

    @staticmethod
    def protection_evidence():
        return {
            "kind": "PROTECTION_VERIFICATION", "independent_check": True,
            "source": "BLOFIN_DEMO_READ_ONLY", "verified_at_utc": "2026-11-15T12:05:00Z",
            "position_id": "position-123", "position_side": "LONG",
            "position_quantity": 0.1, "entry_price": 65000,
            "protection": {
                "tp": {"order_id": "tp-123", "status": "ACTIVE", "position_id": "position-123", "side": "SELL", "quantity": 0.1, "trigger_price": 70000},
                "sl": {"order_id": "sl-123", "status": "ACTIVE", "position_id": "position-123", "side": "SELL", "quantity": 0.1, "trigger_price": 62000},
            },
        }

    def test_unprotected_position_cannot_become_managed_without_tp_sl_proof(self):
        self._open_unprotected_position()
        with self.assertRaises(TransitionValidationError):
            self.transition("managed-without-proof", "POSITION_MANAGED")
        self.assertEqual(self.store.get_intent("intent-state")["current_state"], "POSITION_OPEN_UNPROTECTED")

        result = self.transition(
            "managed-with-proof", "POSITION_MANAGED",
            event_type="PROTECTION_VERIFIED", evidence=self.protection_evidence(),
        )
        self.assertEqual(result["to_state"], "POSITION_MANAGED")

    def test_protection_evidence_must_match_position_and_size(self):
        self._open_unprotected_position()
        bad = self.protection_evidence()
        bad["protection"]["sl"]["position_id"] = "different-position"
        with self.assertRaises(TransitionValidationError):
            self.transition("bad-protection-position", "POSITION_MANAGED", evidence=bad)

        bad = self.protection_evidence()
        bad["protection"]["sl"]["quantity"] = 0.05
        with self.assertRaises(TransitionValidationError):
            self.transition("bad-protection-size", "POSITION_MANAGED", evidence=bad)

        bad = self.protection_evidence()
        bad["protection"]["tp"]["side"] = "BUY"
        with self.assertRaises(TransitionValidationError):
            self.transition("bad-protection-side", "POSITION_MANAGED", evidence=bad)

        bad = self.protection_evidence()
        bad["protection"]["sl"]["trigger_price"] = 66000
        with self.assertRaises(TransitionValidationError):
            self.transition("bad-protection-trigger", "POSITION_MANAGED", evidence=bad)

    def test_terminal_state_rejects_further_transitions(self):
        self.transition("reject-intent", "REJECTED")
        with self.assertRaises(TransitionValidationError):
            self.transition("after-terminal", "VALIDATED")

    def test_event_insert_failure_rolls_back_projection_update(self):
        self.transition("validate-before-fault", "VALIDATED")
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                """CREATE TRIGGER reject_submitting BEFORE INSERT ON trade_events
                   WHEN NEW.to_state = 'SUBMITTING' BEGIN SELECT RAISE(ABORT, 'forced event failure'); END"""
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(sqlite3.IntegrityError):
            self.transition("failed-submitting", "SUBMITTING")
        self.assertEqual(self.store.get_intent("intent-state")["current_state"], "VALIDATED")
        self.assertEqual(len(self.store.list_events("intent-state")), 2)


if __name__ == "__main__":
    unittest.main()
