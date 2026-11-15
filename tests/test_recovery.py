from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from crypto_super_radar.persistent_event_store import EventChainIntegrityError, PersistentEventStore


class PersistentEventStoreRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "persistent-events.sqlite3"
        self.store = PersistentEventStore(self.path)
        self.intent = self.store.register_or_fetch_intent(
            intent_id="intent-restart", client_order_id="client-restart",
            payload={"symbol": "ETH-USDT", "side": "SELL", "quantity": 0.5},
            timestamp_utc="2026-11-15T11:00:00Z",
        )
        self.store.commit_transition_atomic(
            intent_id="intent-restart", client_order_id="client-restart",
            payload_hash=self.intent["payload_hash"], to_state="VALIDATED",
            event_type="VALIDATION", event_id="restart-validated",
            timestamp_utc="2026-11-15T11:00:01Z",
        )
        self.store.commit_transition_atomic(
            intent_id="intent-restart", client_order_id="client-restart",
            payload_hash=self.intent["payload_hash"], to_state="SUBMITTING",
            event_type="SUBMIT_REQUEST", event_id="restart-submitting",
            timestamp_utc="2026-11-15T11:00:02Z",
        )

    def tearDown(self):
        self.store.close()
        self.tempdir.cleanup()

    def test_history_is_recovered_after_store_restart(self):
        expected_hashes = [item["event_hash"] for item in self.store.list_events("intent-restart")]
        self.store.close()
        restarted = PersistentEventStore(self.path)
        try:
            recovered = restarted.get_intent("intent-restart")
            events = restarted.list_events("intent-restart")
            self.assertEqual(recovered["current_state"], "SUBMITTING")
            self.assertEqual([item["event_hash"] for item in events], expected_hashes)
            self.assertEqual(restarted.verify_event_chain("intent-restart")["status"], "PASS")
        finally:
            restarted.close()

    def test_file_database_uses_wal_mode(self):
        connection = sqlite3.connect(self.path)
        try:
            mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual(str(mode).lower(), "wal")
        finally:
            connection.close()

    def test_recovery_checks_reconciliation_event_exchange_id_consistency(self):
        self.store.commit_transition_atomic(
            intent_id="intent-restart", client_order_id="client-restart",
            payload_hash=self.intent["payload_hash"], to_state="UNKNOWN",
            event_type="TRANSPORT_TIMEOUT", event_id="restart-unknown",
            exchange_order_id=None, evidence={"reason": "timeout"},
            timestamp_utc="2026-11-15T11:00:03Z",
        )
        self.store.commit_transition_atomic(
            intent_id="intent-restart", client_order_id="client-restart",
            payload_hash=self.intent["payload_hash"], to_state="ACKNOWLEDGED",
            event_type="RECONCILIATION", event_id="restart-reconciled",
            exchange_order_id="exchange-order-7",
            evidence={
                "kind": "RECONCILIATION", "verified": True,
                "source": "mock_order_query", "reference_id": "mock-query-1",
                "observed_at_utc": "2026-11-15T11:00:04Z",
                "exchange_status": "OPEN", "exchange_order_id": "exchange-order-7",
            },
            timestamp_utc="2026-11-15T11:00:04Z",
        )
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("DROP TRIGGER trade_events_no_update")
            connection.execute(
                "UPDATE trade_events SET evidence_json=? WHERE event_id=?",
                ('{"exchange_order_id":"wrong-order","exchange_status":"OPEN","kind":"RECONCILIATION","observed_at_utc":"2026-11-15T11:00:04Z","reference_id":"mock-query-1","source":"mock_order_query","verified":true}', "restart-reconciled"),
            )
            connection.commit()
        finally:
            connection.close()
        result = self.store.verify_event_chain("intent-restart")
        self.assertEqual(result["status"], "FAIL")
        self.assertTrue(any("exchange_order_id does not match" in item for item in result["errors"]))

    def test_event_table_rejects_update_and_delete(self):
        connection = sqlite3.connect(self.path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE trade_events SET to_state='FILLED' WHERE seq_num=2")
            connection.rollback()
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM trade_events WHERE seq_num=2")
            connection.rollback()
        finally:
            connection.close()

    def test_hash_chain_detects_modified_event_even_if_trigger_is_bypassed(self):
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("DROP TRIGGER trade_events_no_update")
            connection.execute("UPDATE trade_events SET event_type='TAMPERED_EVENT' WHERE seq_num=2")
            connection.commit()
        finally:
            connection.close()

        result = self.store.verify_event_chain("intent-restart")
        self.assertEqual(result["status"], "FAIL")
        self.assertTrue(any("event hash mismatch" in item for item in result["errors"]))
        with self.assertRaises(EventChainIntegrityError):
            self.store.get_intent("intent-restart")

    def test_recovery_detects_sequence_gap(self):
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("DROP TRIGGER trade_events_no_delete")
            connection.execute("DELETE FROM trade_events WHERE seq_num=2")
            connection.commit()
        finally:
            connection.close()
        result = self.store.verify_event_chain("intent-restart")
        self.assertEqual(result["status"], "FAIL")
        self.assertTrue(any("sequence gap" in item for item in result["errors"]))

    def test_intent_identity_columns_are_immutable_but_projection_can_advance(self):
        connection = sqlite3.connect(self.path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE trade_intents SET payload_hash='bad' WHERE intent_id='intent-restart'")
            connection.rollback()
            connection.execute("UPDATE trade_intents SET current_state='VALIDATED' WHERE intent_id='intent-restart'")
            connection.rollback()
        finally:
            connection.close()

    def test_recovery_detects_projection_that_disagrees_with_last_event(self):
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("UPDATE trade_intents SET current_state='FILLED' WHERE intent_id='intent-restart'")
            connection.commit()
        finally:
            connection.close()
        result = self.store.verify_event_chain("intent-restart")
        self.assertEqual(result["status"], "FAIL")
        self.assertTrue(any("last event state" in item for item in result["errors"]))


if __name__ == "__main__":
    unittest.main()
