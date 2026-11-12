from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest

from crypto_super_radar.paper_execution import (
    IdempotencyConflict,
    OrderStateTransitionError,
    PaperExecutionEventLedger,
    make_paper_client_order_id,
)


class PaperExecutionEventLedgerTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.ledger = PaperExecutionEventLedger(self.db)

    def tearDown(self):
        self.db.close()

    def create_and_validate(self, *, order_id="paper-entry-test", quantity=2.0):
        self.ledger.append_event(
            client_order_id=order_id, idempotency_key=f"{order_id}:created",
            asset="cex:BTCUSDT", side="BUY", status="CREATED",
            quantity=quantity, source="PAPER_SIMULATOR", occurred_at=100,
        )
        self.ledger.append_event(
            client_order_id=order_id, idempotency_key=f"{order_id}:validated",
            asset="cex:BTCUSDT", side="BUY", status="VALIDATED",
            quantity=quantity, source="PAPER_SIMULATOR", occurred_at=100,
        )

    def test_validates_partial_fill_then_full_fill(self):
        order_id = "paper-entry-partial"
        self.create_and_validate(order_id=order_id, quantity=2.0)
        partial = self.ledger.append_event(
            client_order_id=order_id, idempotency_key=f"{order_id}:partial-1",
            asset="cex:BTCUSDT", side="BUY", status="PARTIALLY_FILLED",
            quantity=2.0, filled_quantity=0.75, fill_price=100.0,
            fee_paid_usd=0.06, source="PAPER_SIMULATOR", occurred_at=101,
        )
        self.assertEqual(partial["status"], "PARTIALLY_FILLED")
        filled = self.ledger.append_event(
            client_order_id=order_id, idempotency_key=f"{order_id}:filled",
            asset="cex:BTCUSDT", side="BUY", status="FILLED",
            quantity=2.0, filled_quantity=2.0, fill_price=100.2,
            fee_paid_usd=0.16, source="PAPER_SIMULATOR", occurred_at=102,
        )
        self.assertEqual(filled["status"], "FILLED")
        self.assertEqual(self.ledger.order_state(order_id)["filled_quantity"], 2.0)

    def test_rejects_invalid_transition_and_terminal_state_mutation(self):
        with self.assertRaises(OrderStateTransitionError):
            self.ledger.append_event(
                client_order_id="paper-skip", idempotency_key="paper-skip:filled",
                asset="cex:BTCUSDT", side="BUY", status="FILLED",
                quantity=1.0, filled_quantity=1.0, fill_price=100.0,
                fee_paid_usd=0.1,
            )
        self.ledger.record_simulated_fill(
            client_order_id="paper-terminal", asset="cex:BTCUSDT", side="BUY",
            quantity=1.0, fill_price=100.0, fee_paid_usd=0.1,
            source="PAPER_SIMULATOR", reason="TEST",
        )
        with self.assertRaises(OrderStateTransitionError):
            self.ledger.append_event(
                client_order_id="paper-terminal", idempotency_key="paper-terminal:cancel",
                asset="cex:BTCUSDT", side="BUY", status="CANCELED",
                quantity=1.0, filled_quantity=1.0, source="PAPER_SIMULATOR",
            )

    def test_idempotent_retry_returns_same_event_and_conflict_is_rejected(self):
        first = self.ledger.append_event(
            client_order_id="paper-idem", idempotency_key="paper-idem:created",
            asset="cex:BTCUSDT", side="BUY", status="CREATED", quantity=1.0,
            source="PAPER_SIMULATOR", occurred_at=100,
        )
        repeated = self.ledger.append_event(
            client_order_id="paper-idem", idempotency_key="paper-idem:created",
            asset="cex:BTCUSDT", side="BUY", status="CREATED", quantity=1.0,
            source="PAPER_SIMULATOR", occurred_at=100,
        )
        self.assertEqual(first["event_id"], repeated["event_id"])
        self.assertTrue(first["inserted"])
        self.assertFalse(repeated["inserted"])
        self.assertEqual(self.ledger.event_count(), 1)
        with self.assertRaises(IdempotencyConflict):
            self.ledger.append_event(
                client_order_id="paper-idem", idempotency_key="paper-idem:created",
                asset="cex:BTCUSDT", side="BUY", status="CREATED", quantity=2.0,
                source="PAPER_SIMULATOR", occurred_at=100,
            )

    def test_order_identity_and_cumulative_fill_are_immutable(self):
        self.create_and_validate(order_id="paper-invariant", quantity=2.0)
        with self.assertRaises(OrderStateTransitionError):
            self.ledger.append_event(
                client_order_id="paper-invariant", idempotency_key="paper-invariant:wrong-asset",
                asset="cex:ETHUSDT", side="BUY", status="SUBMITTED",
                quantity=2.0, source="PAPER_SIMULATOR",
            )
        with self.assertRaises(OrderStateTransitionError):
            self.ledger.append_event(
                client_order_id="paper-invariant", idempotency_key="paper-invariant:wrong-qty",
                asset="cex:BTCUSDT", side="BUY", status="SUBMITTED",
                quantity=3.0, source="PAPER_SIMULATOR",
            )

    def test_event_rows_are_append_only(self):
        event = self.ledger.append_event(
            client_order_id="paper-immutable", idempotency_key="paper-immutable:created",
            asset="cex:BTCUSDT", side="BUY", status="CREATED", quantity=1.0,
            source="PAPER_SIMULATOR",
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                "UPDATE paper_execution_events SET status='FILLED' WHERE event_id=?",
                (event["event_id"],),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                "DELETE FROM paper_execution_events WHERE event_id=?",
                (event["event_id"],),
            )

    def test_simulated_fill_records_created_validated_filled(self):
        order_id = "paper-simulated"
        final = self.ledger.record_simulated_fill(
            client_order_id=order_id, asset="cex:BTCUSDT", side="BUY",
            quantity=1.25, fill_price=100.5, fee_paid_usd=0.1,
            source="OBSERVED_QUOTE_PAPER_FILL", reason="PAPER_ENTRY",
            occurred_at=123,
        )
        self.assertEqual(final["status"], "FILLED")
        events = self.ledger.recent_events(10, client_order_id=order_id)
        self.assertEqual(
            [row["status"] for row in reversed(events)],
            ["CREATED", "VALIDATED", "FILLED"],
        )
        self.assertEqual(self.ledger.latest_orders(1)[0]["client_order_id"], order_id)

    def test_events_survive_database_reconnect(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "paper-ledger.sqlite3")
            first_db = sqlite3.connect(path)
            first_db.row_factory = sqlite3.Row
            first = PaperExecutionEventLedger(first_db)
            with first_db:
                first.record_simulated_fill(
                    client_order_id="paper-reconnect", asset="cex:ETHUSDT", side="SELL",
                    quantity=0.5, fill_price=2000.0, fee_paid_usd=0.4,
                    source="PAPER_SIMULATOR", reason="RECONNECT_TEST", occurred_at=456,
                )
            first_db.close()
            second_db = sqlite3.connect(path)
            second_db.row_factory = sqlite3.Row
            second = PaperExecutionEventLedger(second_db)
            state = second.order_state("paper-reconnect")
            self.assertEqual(state["status"], "FILLED")
            self.assertEqual(state["filled_quantity"], 0.5)
            self.assertEqual(second.event_count(), 3)
            second_db.close()

    def test_replay_reconciles_valid_event_history(self):
        self.ledger.record_simulated_fill(
            client_order_id="paper-replay", asset="cex:BTCUSDT", side="BUY",
            quantity=1.0, fill_price=101.0, fee_paid_usd=0.1,
            source="PAPER_SIMULATOR", reason="REPLAY_TEST", occurred_at=123,
        )
        result = self.ledger.replay_order_states()
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["event_count"], 3)
        self.assertEqual(result["order_count"], 1)
        self.assertEqual(result["orders"]["paper-replay"]["status"], "FILLED")
        self.assertEqual(result["errors"], [])

    def test_replay_detects_payload_tampering_without_rewriting_history(self):
        self.ledger.record_simulated_fill(
            client_order_id="paper-tamper", asset="cex:BTCUSDT", side="BUY",
            quantity=1.0, fill_price=101.0, fee_paid_usd=0.1,
            source="PAPER_SIMULATOR", reason="TAMPER_TEST", occurred_at=123,
        )
        # Simulate external file corruption after dropping only the test DB trigger.
        self.db.execute("DROP TRIGGER paper_execution_events_no_update")
        self.db.execute(
            "UPDATE paper_execution_events SET payload_json='{}' WHERE idempotency_key=?",
            ("paper-tamper:filled",),
        )
        result = self.ledger.replay_order_states()
        self.assertEqual(result["status"], "FAIL")
        self.assertTrue(any("payload does not match" in error for error in result["errors"]))

    def test_client_order_id_is_stable_and_leg_scoped(self):
        entry = make_paper_client_order_id(
            "cex:BTCUSDT", "2026-11-12T00:00:00+00:00", "ENTRY"
        )
        self.assertEqual(
            entry,
            make_paper_client_order_id(
                "cex:BTCUSDT", "2026-11-12T00:00:00+00:00", "ENTRY"
            ),
        )
        self.assertNotEqual(
            entry,
            make_paper_client_order_id(
                "cex:BTCUSDT", "2026-11-12T00:00:00+00:00", "EXIT"
            ),
        )


if __name__ == "__main__":
    unittest.main()
