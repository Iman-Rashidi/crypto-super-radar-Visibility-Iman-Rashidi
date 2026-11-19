from __future__ import annotations

import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import requests

from crypto_super_radar.notion_event_sync import NotionEventOutbox


def event(**overrides):
    value = {
        "event_id": "cycle-1:BTC-USDT:NO_TRADE",
        "cycle_id": "cycle-1",
        "event_type": "NO_TRADE",
        "actor": "Robot",
        "status": "SUCCESS",
        "market": "Futures",
        "symbol": "BTC-USDT",
        "timeframe": "5m",
        "signal": "NO_TRADE",
        "reason": "No valid closed-candle setup.",
        "details": "orders_submitted=false",
    }
    value.update(overrides)
    return value


class FakeResponse:
    def __init__(self, payload=None, status_code=200, error=None):
        self.payload = payload or {}
        self.status_code = status_code
        self.error = error

    def raise_for_status(self):
        if self.error:
            raise self.error

    def json(self):
        return self.payload


class NotionEventOutboxTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.outbox = NotionEventOutbox(self.db)
        self.env = patch.dict("os.environ", {"NOTION_API_TOKEN": "test-secret"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.db.close()

    def test_enqueue_is_idempotent(self):
        self.assertTrue(self.outbox.enqueue(event()))
        self.assertFalse(self.outbox.enqueue(event()))
        self.assertEqual(self.outbox.pending_count(), 1)

    def test_missing_token_keeps_events_pending_without_network(self):
        with patch.dict("os.environ", {"NOTION_API_TOKEN": ""}):
            with patch("crypto_super_radar.notion_event_sync.requests.post") as post:
                result = self.outbox.flush_pending(now=1_800_000_000)
        self.assertEqual(result["status"], "NOT_CONFIGURED")
        self.assertEqual(result["queued"], 0)
        post.assert_not_called()

    def test_legacy_notion_token_alias_is_accepted(self):
        with patch.dict("os.environ", {"NOTION_API_TOKEN": "", "NOTION_TOKEN": "legacy-test-secret"}):
            self.assertTrue(self.outbox.status_snapshot()["token_configured"])
            with patch("crypto_super_radar.notion_event_sync.requests.post") as post:
                result = self.outbox.flush_pending(now=1_800_000_000)
        self.assertEqual(result["status"], "CONFIGURED")
        post.assert_not_called()

    def test_existing_notion_page_is_reconciled_without_duplicate_create(self):
        self.outbox.enqueue(event())
        with patch(
            "crypto_super_radar.notion_event_sync.requests.post",
            return_value=FakeResponse({"results": [{"id": "notion-page-1"}]}),
        ) as post:
            result = self.outbox.flush_pending(now=1_800_000_000)
        self.assertEqual(result["synced_this_run"], 1)
        self.assertEqual(result["pending"], 0)
        self.assertEqual(result["synced"], 1)
        self.assertEqual(post.call_count, 1)
        self.assertIn("/data_sources/", post.call_args.args[0])
        self.assertFalse(post.call_args.kwargs["allow_redirects"])

    def test_create_page_after_empty_idempotency_query(self):
        self.outbox.enqueue(event())
        with patch(
            "crypto_super_radar.notion_event_sync.requests.post",
            side_effect=[
                FakeResponse({"results": []}),
                FakeResponse({"id": "notion-page-2"}),
            ],
        ) as post:
            result = self.outbox.flush_pending(now=1_800_000_000)
        self.assertEqual(result["synced_this_run"], 1)
        self.assertEqual(post.call_count, 2)
        self.assertTrue(all(not call.kwargs["allow_redirects"] for call in post.call_args_list))
        create_body = post.call_args.kwargs["json"]
        self.assertEqual(
            create_body["parent"]["data_source_id"],
            "41a6c893-0218-81ce-9359-000bf3e47b44",
        )
        self.assertEqual(
            create_body["properties"]["نوع رویداد"]["select"]["name"],
            "NO_TRADE",
        )
        self.assertEqual(
            create_body["properties"]["سیگنال ربات"]["select"]["name"],
            "NO_TRADE",
        )
        self.assertIn("زمان رویداد UTC", create_body["properties"])
        self.assertIn("زمان آخرین تلاش Sync", create_body["properties"])
        self.assertNotIn("date:زمان رویداد UTC", create_body["properties"])

    def test_http_failure_keeps_event_pending_and_applies_backoff(self):
        self.outbox.enqueue(event())
        error = requests.HTTPError("Notion unavailable")
        error.response = SimpleNamespace(status_code=503, headers={})
        with patch(
            "crypto_super_radar.notion_event_sync.requests.post",
            side_effect=error,
        ):
            result = self.outbox.flush_pending(now=1_800_000_000)
        self.assertEqual(result["failed_this_run"], 1)
        self.assertEqual(result["pending"], 1)
        self.assertEqual(result["status"], "DEGRADED")
        self.assertEqual(result["retrying"], 1)
        self.assertEqual(result["last_error_type"], "HTTPError")
        self.assertEqual(result["last_http_status"], 503)
        self.assertEqual(result["recent_errors"][0]["http_status"], 503)
        row = self.db.execute(
            "SELECT attempts, next_attempt_at, last_error_type, last_http_status FROM csr_notion_event_outbox"
        ).fetchone()
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(row["next_attempt_at"], 1_800_000_005)
        self.assertEqual(row["last_error_type"], "HTTPError")
        self.assertEqual(row["last_http_status"], 503)

    def test_position_sync_success_is_written_to_challenge1_audit_chain(self):
        position_event = event(
            event_id="position-review-success",
            cycle_id="experience-1",
            event_type="POSITION_REVIEW",
            experience_id="experience-1",
            position_id="position-1",
        )
        self.outbox.enqueue(position_event)
        with (
            patch(
                "crypto_super_radar.notion_event_sync.requests.post",
                return_value=FakeResponse({"id": "notion-position-page-1"}),
            ),
            patch(
                "crypto_super_radar.notion_event_sync._record_position_notion_sync_audit",
                return_value={"status": "RECORDED"},
            ) as audit,
        ):
            result = self.outbox.flush_pending(now=1_800_000_000)
        self.assertEqual(result["synced_this_run"], 1)
        audit.assert_called_once()
        self.assertEqual(audit.call_args.args[0]["event_id"], "position-review-success")
        self.assertEqual(audit.call_args.kwargs["status"], "SYNCED")
        self.assertEqual(audit.call_args.kwargs["attempt"], 1)
        self.assertEqual(
            audit.call_args.kwargs["notion_page_id"], "notion-position-page-1",
        )
        state = self.outbox.get_event_status("position-review-success")
        self.assertEqual(state["status"], "SYNCED")
        self.assertEqual(state["notion_page_id"], "notion-position-page-1")
        self.assertIsNotNone(state["synced_at_utc"])

    def test_position_sync_http_failure_is_written_to_challenge1_audit_chain(self):
        position_event = event(
            event_id="position-review-failed",
            cycle_id="experience-2",
            event_type="POSITION_REVIEW",
            experience_id="experience-2",
            position_id="position-2",
        )
        self.outbox.enqueue(position_event)
        error = requests.HTTPError("Notion unavailable")
        error.response = SimpleNamespace(status_code=503, headers={})
        with (
            patch(
                "crypto_super_radar.notion_event_sync.requests.post",
                side_effect=error,
            ),
            patch(
                "crypto_super_radar.notion_event_sync._record_position_notion_sync_audit",
                return_value={"status": "RECORDED"},
            ) as audit,
        ):
            result = self.outbox.flush_pending(now=1_800_000_000)
        self.assertEqual(result["failed_this_run"], 1)
        audit.assert_called_once()
        self.assertEqual(audit.call_args.kwargs["status"], "PENDING_RETRY")
        self.assertEqual(audit.call_args.kwargs["error_type"], "HTTPError")
        self.assertEqual(audit.call_args.kwargs["http_status"], 503)
        state = self.outbox.get_event_status("position-review-failed")
        self.assertEqual(state["status"], "PENDING")
        self.assertEqual(state["last_error_type"], "HTTPError")
        self.assertEqual(state["last_http_status"], 503)

    def test_missing_notion_token_is_an_explicit_position_sync_state(self):
        position_event = event(
            event_id="position-review-no-token",
            cycle_id="experience-3",
            event_type="POSITION_REVIEW",
            experience_id="experience-3",
            position_id="position-3",
        )
        self.outbox.enqueue(position_event)
        with (
            patch.dict("os.environ", {"NOTION_API_TOKEN": "", "NOTION_TOKEN": ""}),
            patch(
                "crypto_super_radar.notion_event_sync._record_position_notion_sync_audit",
                return_value={"status": "RECORDED"},
            ) as audit,
        ):
            result = self.outbox.flush_pending(now=1_800_000_000)
        self.assertEqual(result["status"], "NOT_CONFIGURED")
        self.assertEqual(result["position_audit_events"], 1)
        audit.assert_called_once()
        self.assertEqual(audit.call_args.kwargs["status"], "NOT_CONFIGURED")
        self.assertEqual(audit.call_args.kwargs["error_type"], "NOTION_TOKEN_MISSING")

    def test_missing_outbox_event_status_is_reported(self):
        result = self.outbox.get_event_status("missing-event")
        self.assertEqual(result["status"], "NOT_FOUND")

    def test_invalid_event_type_is_rejected_before_persistence(self):
        with self.assertRaises(ValueError):
            self.outbox.enqueue(event(event_type="FAKE_EVENT"))
        self.assertEqual(self.outbox.pending_count(), 0)


if __name__ == "__main__":
    unittest.main()
