from __future__ import annotations

import sqlite3
import unittest

from crypto_super_radar.challenge1_audit import Challenge1AuditRecorder
from crypto_super_radar.notion_event_sync import NotionEventOutbox


class Challenge1AuditRecorderTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.recorder = Challenge1AuditRecorder(connection=self.connection)

    def tearDown(self):
        self.recorder.close()
        self.connection.close()

    def test_decision_chain_links_entry_and_later_exit_by_experience_id(self):
        first = self.recorder.append_event(
            cycle_id="job-001",
            phase="STRATEGY_DECISION",
            event_key="decision",
            experience_id="experience-001",
            details={"signal": "LONG", "entry": 100.0, "challenge1_evidence": {"pattern": "bullish"}},
        )
        second = self.recorder.append_event(
            cycle_id="experience-001",
            phase="POSITION_EXIT_EVALUATION",
            event_key="position-001:minute-1",
            experience_id="experience-001",
            position_id="position-001",
            details={"decision": "HOLD", "reason": "minimum profit not reached"},
        )
        report = self.recorder.list_related("job-001")
        self.assertEqual(first["cycle_id"], "job-001")
        self.assertEqual(second["cycle_id"], "job-001")
        self.assertEqual(report["event_count"], 2)
        self.assertEqual(report["integrity"]["status"], "PASS")
        self.assertEqual(report["events"][1]["phase"], "POSITION_EXIT_EVALUATION")

    def test_event_key_is_idempotent_and_hash_chain_verifies(self):
        args = {
            "cycle_id": "job-002", "phase": "ASSET_SELECTED",
            "event_key": "asset-selected", "details": {"instrument_id": "BTC-USDT"},
        }
        first = self.recorder.append_event(**args)
        duplicate = self.recorder.append_event(**args)
        self.assertTrue(first["inserted"])
        self.assertFalse(duplicate["inserted"])
        self.assertEqual(first["event_id"], duplicate["event_id"])
        self.assertEqual(self.recorder.verify_integrity(["job-002"])["status"], "PASS")

    def test_audit_rows_cannot_be_updated_or_deleted(self):
        self.recorder.append_event(
            cycle_id="job-003", phase="AI_REVIEW_RESULT",
            event_key="review", details={"reviewer_id": "nemotron", "status": "APPROVED"},
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "UPDATE csr_challenge1_audit_events SET phase='EDITED'"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("DELETE FROM csr_challenge1_audit_events")

    def test_sensitive_field_names_are_redacted_before_storage(self):
        self.recorder.append_event(
            cycle_id="job-004", phase="ANALYSIS_STARTED",
            event_key="start",
            details={"api_key": "do-not-store", "market": {"authorization": "private", "price": 12.5}},
        )
        report = self.recorder.list_related("job-004")
        details = report["events"][0]["payload"]["details"]
        self.assertEqual(details["api_key"], "[REDACTED]")
        self.assertEqual(details["market"]["authorization"], "[REDACTED]")
        self.assertEqual(details["market"]["price"], 12.5)

    def test_notion_projection_accepts_challenge1_audit_event_types(self):
        outbox = NotionEventOutbox(connection=self.connection)
        payload = {
            "event_id": "test-ch1-audit-event",
            "event_type": "CHALLENGE1_DECISION_TRACE",
            "actor": "Robot",
            "status": "SUCCESS",
            "market": "Futures",
            "symbol": "BTC-USDT",
            "signal": "LONG",
            "details": "pre-decision strategy evidence",
        }
        normalized = outbox.normalize_event(payload)
        self.assertEqual(normalized["event_type"], "CHALLENGE1_DECISION_TRACE")
        outbox.close()


if __name__ == "__main__":
    unittest.main()
