from __future__ import annotations

import unittest
from unittest.mock import patch

from crypto_super_radar.challenge1_sandbox_worker import (
    build_blocked_event,
    build_sandbox_event,
    run_worker,
)


class Challenge1SandboxWorkerTests(unittest.TestCase):
    def test_successful_report_becomes_a_deterministic_read_only_queue_event(self):
        report = {
            "report_version": "CHALLENGE1_AI_SANDBOX_V1",
            "data_source": "LOCAL_SQLITE_REPLAY_DOWNLOADED_FROM_BLOFIN_DEMO_PUBLIC_API",
            "network_order_writes": False,
            "periods": {"start_utc": "2026-01-01T00:00:00Z"},
            "equity": {"net_pnl_usdt": 12.5},
            "trade_metrics": {"closed_trades": 3, "wins": 2, "losses": 1},
            "walk_forward_audit": {"status": "PASS", "purge_window_minutes": 195},
            "model_calibration": {"VALIDATION": {"status": "CALCULATED"}},
            "ai_audit": {"trained": True, "model_sha256": "abc"},
        }
        first = build_sandbox_event(report, now_ms=1000)
        second = build_sandbox_event(report, now_ms=1000)
        self.assertEqual(first, second)
        self.assertEqual(first["producer"], "AI_SANDBOX")
        self.assertEqual(first["event_type"], "REPLAY_REPORT")
        self.assertEqual(first["payload"]["network_order_writes"], True)
        self.assertEqual(first["payload"]["trade_metrics"]["closed_trades"], 3)
        self.assertNotIn("trade_ledger", first["payload"])

    def test_blocked_walk_forward_is_not_published_as_success(self):
        event = build_sandbox_event(
            {"walk_forward_audit": {"status": "BLOCKED"}, "network_order_writes": False},
            now_ms=1000,
        )
        self.assertEqual(event["event_type"], "REPLAY_BLOCKED")
        self.assertEqual(event["payload"]["replay_status"], "BLOCKED")

    def test_blocked_event_excludes_raw_exception_message(self):
        event = build_blocked_event("ReplayDownloadError: SECRET=do-not-copy", now_ms=1000)
        self.assertEqual(event["event_type"], "REPLAY_BLOCKED")
        self.assertNotIn("SECRET", str(event["payload"]))
        self.assertNotIn("do-not-copy", str(event["payload"]))

    def test_worker_refuses_local_sqlite_as_shared_cross_worker_backend(self):
        with patch(
            "crypto_super_radar.challenge1_sandbox_worker.storage_backend",
            return_value="LOCAL_SQLITE",
        ):
            with self.assertRaisesRegex(RuntimeError, "shared Turso storage is required"):
                run_worker(db_path="unused.sqlite3")


if __name__ == "__main__":
    unittest.main()
