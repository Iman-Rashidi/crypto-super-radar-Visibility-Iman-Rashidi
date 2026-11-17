from __future__ import annotations

import io
import sqlite3
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

from crypto_super_radar.notion_event_sync import NotionEventOutbox
from crypto_super_radar import web_app


class OperationalEventRecordingTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.outbox = NotionEventOutbox(self.db)

    def tearDown(self):
        self.db.close()

    def _event(self, event_id, event_type, status="SUCCESS"):
        return {
            "event_id": event_id, "cycle_id": event_id, "event_type": event_type,
            "actor": "Operator", "status": status, "market": "Futures",
            "symbol": "BTC-USDT", "timeframe": "MANUAL", "signal": "UNKNOWN",
            "ai_decision": "NOT_CALLED", "reason": "Audit test.",
            "details": "environment=BLOFIN_DEMO",
        }

    def test_existing_manual_options_are_accepted_and_idempotent(self):
        for i, event_type in enumerate((
            "MANUAL_ORDER_CANCELED", "MANUAL_POSITION_CLOSE_REQUESTED", "MANUAL_ACTION_FAILED",
        )):
            payload = self._event(f"manual-audit-{i}", event_type)
            self.assertTrue(self.outbox.enqueue(payload))
            self.assertFalse(self.outbox.enqueue(payload))
        self.assertEqual(self.outbox.pending_count(), 3)

    def test_sync_status_exposes_retries_and_last_error(self):
        payload = self._event("manual-retry-1", "MANUAL_ACTION_FAILED", "FAILED")
        self.outbox.enqueue(payload)
        self.db.execute(
            "UPDATE csr_notion_event_outbox SET attempts=3, last_error_type='HTTPError', last_http_status=503 WHERE event_id=?",
            (payload["event_id"],),
        )
        self.db.commit()
        status = self.outbox.status_snapshot()
        self.assertEqual(status["pending"], 1)
        self.assertEqual(status["retrying"], 1)
        self.assertEqual(status["max_attempts"], 3)
        self.assertEqual(status["last_error_type"], "HTTPError")
        self.assertEqual(status["last_http_status"], 503)

    def test_operator_event_helper_uses_supported_notion_values(self):
        mock_outbox = MagicMock()
        captured = {}
        def enqueue(payload):
            captured.update(payload)
            return True
        mock_outbox.enqueue.side_effect = enqueue
        output = io.StringIO()
        with patch.object(web_app, "NotionEventOutbox", return_value=mock_outbox):
            with patch.object(web_app, "storage_backend", return_value="TURSO_CLOUD_SQLITE"):
                with redirect_stdout(output):
                    result = web_app._queue_blofin_operator_event(
                        event_id="manual-close-accepted-1",
                        event_type="MANUAL_POSITION_CLOSE_REQUESTED",
                        status="SUCCESS", symbol="BTC-USDT",
                        position_id="position-123",
                        reason="Exchange accepted close request.",
                        details={"operation_stage": "EXCHANGE_ACCEPTED"},
                    )
        self.assertEqual(result["status"], "QUEUED")
        self.assertEqual(captured["actor"], "Operator")
        self.assertEqual(captured["event_type"], "MANUAL_POSITION_CLOSE_REQUESTED")
        self.assertEqual(captured["position_id"], "position-123")
        self.assertIn("EXCHANGE_ACCEPTED", captured["details"])
        self.assertIn("CSR_NOTION_EVENT_QUEUED", output.getvalue())
        mock_outbox.close.assert_called_once()

    def test_job_event_mapper_adds_scan_completion(self):
        job = {
            "status": "completed", "stage": "NO_TRADE",
            "result": {"status":"NO_TRADE","signal":"NO_TRADE",
                       "reason_fa":"No confirmed pattern.",
                       "orders_submitted":False,"trade_executed":False},
        }
        events = web_app._blofin_job_notion_events("cycle-scan-end", "BTC-USDT", job)
        completed = [row for row in events if row["event_type"] == "SCAN_COMPLETED"]
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["actor"], "Robot")
        self.assertEqual(completed[0]["event_id"], "cycle-scan-end:ROBOT:SCAN_COMPLETED")


if __name__ == "__main__":
    unittest.main()
