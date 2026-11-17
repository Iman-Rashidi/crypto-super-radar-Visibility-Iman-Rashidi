from __future__ import annotations

import sqlite3
import unittest
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
        with patch(
            "crypto_super_radar.notion_event_sync.requests.post",
            side_effect=error,
        ):
            result = self.outbox.flush_pending(now=1_800_000_000)
        self.assertEqual(result["failed_this_run"], 1)
        self.assertEqual(result["pending"], 1)
        row = self.db.execute(
            "SELECT attempts, next_attempt_at, last_error_type FROM csr_notion_event_outbox"
        ).fetchone()
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(row["next_attempt_at"], 1_800_000_005)
        self.assertEqual(row["last_error_type"], "HTTPError")

    def test_invalid_event_type_is_rejected_before_persistence(self):
        with self.assertRaises(ValueError):
            self.outbox.enqueue(event(event_type="FAKE_EVENT"))
        self.assertEqual(self.outbox.pending_count(), 0)


if __name__ == "__main__":
    unittest.main()
