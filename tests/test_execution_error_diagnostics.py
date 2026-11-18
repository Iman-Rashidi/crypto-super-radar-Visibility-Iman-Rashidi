from __future__ import annotations

import json
import sqlite3
import unittest

from crypto_super_radar.execution_diagnostics import build_execution_error_diagnostic


class ExecutionErrorDiagnosticTests(unittest.TestCase):
    def test_operational_error_context_is_complete_and_secrets_are_redacted(self):
        environment = {
            "BLOFIN_DEMO_TRADE_API_SECRET": "env-secret-value-123",
            "NOTION_API_TOKEN": "notion-token-value-456",
            "UNIFIED_ACCOUNT_KEY": "unified-account-key-value-789",
        }
        try:
            raise sqlite3.OperationalError(
                "database is locked; Bearer bearer-secret-value "
                "token=query-secret-value api_key=assignment-secret-value "
                "env-secret-value-123 notion-token-value-456 unified-account-key-value-789"
            )
        except sqlite3.OperationalError as exc:
            diagnostic = build_execution_error_diagnostic(
                exc,
                failed_operation="learning_memory.record_decision",
                instrument_id="BTC-USDT",
                cycle_id="cycle-123",
                job_id="job-456",
                execution_stage="CHALLENGE_1_PATTERN_ANALYSIS",
                database_backend="TURSO_CLOUD_SQLITE",
                timestamp_utc="2026-11-18T12:00:00+00:00",
                environ=environment,
            )

        serialized = json.dumps(diagnostic, ensure_ascii=False)
        self.assertEqual(diagnostic["error_type"], "OperationalError")
        self.assertIn("database is locked", diagnostic["error_message"])
        self.assertIn("OperationalError", diagnostic["traceback"])
        self.assertEqual(
            diagnostic["failed_operation"], "learning_memory.record_decision"
        )
        self.assertEqual(diagnostic["instrument_id"], "BTC-USDT")
        self.assertEqual(diagnostic["cycle_id"], "cycle-123")
        self.assertEqual(diagnostic["job_id"], "job-456")
        self.assertEqual(
            diagnostic["execution_stage"], "CHALLENGE_1_PATTERN_ANALYSIS"
        )
        self.assertEqual(diagnostic["database_backend"], "TURSO_CLOUD_SQLITE")
        self.assertEqual(
            diagnostic["timestamp_utc"], "2026-11-18T12:00:00+00:00"
        )
        self.assertTrue(diagnostic["error_trace_reference"].startswith("render-stdout:job-456:"))
        for secret in (
            "env-secret-value-123", "notion-token-value-456",
            "bearer-secret-value", "query-secret-value",
            "assignment-secret-value", "unified-account-key-value-789",
        ):
            self.assertNotIn(secret, serialized)
        self.assertIn("[REDACTED]", serialized)


if __name__ == "__main__":
    unittest.main()
