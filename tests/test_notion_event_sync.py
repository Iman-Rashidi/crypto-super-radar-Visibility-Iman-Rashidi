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
        "timeframe": "15m",
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
        with (
            patch(
                "crypto_super_radar.notion_event_sync.requests.post",
                return_value=FakeResponse({"results": [{"id": "notion-page-1"}]}),
            ) as post,
            patch(
                "crypto_super_radar.notion_event_sync.requests.patch",
                return_value=FakeResponse({"id": "notion-page-1"}),
            ) as patch_page,
        ):
            result = self.outbox.flush_pending(now=1_800_000_000)
        self.assertEqual(result["synced_this_run"], 1)
        self.assertEqual(result["pending"], 0)
        self.assertEqual(result["synced"], 1)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(patch_page.call_count, 1)
        self.assertIn("/data_sources/", post.call_args.args[0])
        self.assertIn("/pages/notion-page-1", patch_page.call_args.args[0])

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

    def test_local_reviewer_results_are_durable_but_not_synced_as_separate_notion_pages(self):
        reviewer_result = event(
            event_id="trade-audit:entry:AI:nemotron:RESULT",
            event_type="AI_RECOMMENDATION",
            actor="Nemotron Supervisor",
            signal="LONG",
            ai_decision="OBSERVE_ONLY",
            ai_provider="OpenRouter",
            ai_model="nvidia/nemotron-3-ultra-550b-a55b:free",
            details='{"phase":"RESULT","reviewer_id":"nemotron"}',
        )
        self.assertTrue(self.outbox.enqueue(reviewer_result, sync_to_notion=False))
        stored = self.outbox.get_event(reviewer_result["event_id"])
        self.assertIsNotNone(stored)
        self.assertFalse(stored["_outbox"]["sync_to_notion"])
        self.assertEqual(stored["_outbox"]["sync_status"], "LOCAL_ONLY")
        self.assertEqual(self.outbox.pending_count(), 0)
        snapshot = self.outbox.status_snapshot()
        self.assertEqual(snapshot["local_only"], 1)
        with (
            patch("crypto_super_radar.notion_event_sync.requests.post") as post,
            patch("crypto_super_radar.notion_event_sync.requests.patch") as patch_page,
        ):
            flushed = self.outbox.flush_pending(now=1_800_000_000)
        self.assertEqual(flushed["synced_this_run"], 0)
        post.assert_not_called()
        patch_page.assert_not_called()

    def test_unified_report_upsert_updates_existing_notion_page_in_place(self):
        first = event(
            event_id="CSR-TRADE-SUPERVISION:experience-1",
            event_type="AI_REVIEW_REQUESTED",
            actor="AI Supervisor",
            signal="LONG",
            ai_decision="OBSERVE_ONLY",
            experience_id="experience-1",
            details='{"report_type":"UNIFIED_TRADE_SUPERVISION","phases":{"ENTRY":{"status":"REQUESTED"}}}',
        )
        self.assertTrue(self.outbox.enqueue(first))
        with patch(
            "crypto_super_radar.notion_event_sync.requests.post",
            side_effect=[
                FakeResponse({"results": []}),
                FakeResponse({"id": "notion-trade-page-1"}),
            ],
        ) as post:
            first_sync = self.outbox.flush_pending(now=1_800_000_000)
        self.assertEqual(first_sync["synced_this_run"], 1)
        updated = dict(first)
        updated.update({
            "event_type": "AI_RECOMMENDATION",
            "details": '{"report_type":"UNIFIED_TRADE_SUPERVISION","phases":{"ENTRY":{"status":"COMPLETED"}},"final_trade_opinion":"AWAITING_EXIT_REVIEW"}',
            "ai_suggestion": "ENTRY REVIEW COMPLETED",
        })
        self.assertTrue(self.outbox.enqueue(updated, replace=True))
        stored = self.outbox.get_event(first["event_id"])
        self.assertEqual(stored["details"], updated["details"])
        with (
            patch("crypto_super_radar.notion_event_sync.requests.post") as query,
            patch(
                "crypto_super_radar.notion_event_sync.requests.patch",
                return_value=FakeResponse({"id": "notion-trade-page-1"}),
            ) as patch_page,
        ):
            result = self.outbox.flush_pending(now=1_800_000_010)
        self.assertEqual(result["synced_this_run"], 1)
        query.assert_not_called()
        self.assertEqual(patch_page.call_count, 1)
        body = patch_page.call_args.kwargs["json"]
        self.assertIn("AWAITING_EXIT_REVIEW", body["properties"]["جزئیات و Payload خلاصه"]["rich_text"][0]["text"]["content"])

    def test_unified_report_details_are_chunked_without_truncation(self):
        large_details = '{"report":"' + ("x" * 7800) + '"}'
        payload = self.outbox.normalize_event(event(details=large_details))
        properties = self.outbox._notion_properties(payload, 1)
        rich_text = properties["جزئیات و Payload خلاصه"]["rich_text"]
        self.assertGreater(len(rich_text), 1)
        self.assertEqual("".join(part["text"]["content"] for part in rich_text), large_details)
        self.assertTrue(all(len(part["text"]["content"]) <= 1800 for part in rich_text))

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


    def test_independent_supervisor_actors_are_accepted_for_audit_events(self):
        nemotron = event(
            event_id="cycle-ai:nemotron:result",
            event_type="AI_RECOMMENDATION",
            actor="Nemotron Supervisor",
            status="SUCCESS",
            signal="LONG",
            ai_decision="OBSERVE_ONLY",
            ai_model="nvidia/nemotron-free",
            ai_suggestion="SUPPORTS_DECISION",
        )
        second = event(
            event_id="cycle-ai:second:request",
            event_type="AI_REVIEW_REQUESTED",
            actor="Second AI Supervisor",
            status="PENDING",
            signal="LONG",
            ai_decision="OBSERVE_ONLY",
        )
        self.assertTrue(self.outbox.enqueue(nemotron))
        self.assertTrue(self.outbox.enqueue(second))
        self.assertEqual(self.outbox.pending_count(), 2)



    def test_outbox_redacts_credential_values_and_only_accepts_model_identifiers(self):
        suspicious = event(
            event_id="cycle-ai:secret:request",
            event_type="AI_REVIEW_REQUESTED",
            actor="Nemotron Supervisor",
            ai_model="sk-or-v1-THISISNOTAREALSECRET000000000000nvidia/nemotron-test:free",
            ai_provider="OpenRouter",
            details='{"note":"Bearer EXAMPLE_TOKEN_12345678901234567890"}',
        )
        self.assertTrue(self.outbox.enqueue(suspicious))
        row = self.db.execute(
            "SELECT payload_json FROM csr_notion_event_outbox WHERE event_id=?",
            ("cycle-ai:secret:request",),
        ).fetchone()
        saved = __import__("json").loads(row["payload_json"])
        self.assertEqual(saved["ai_model"], "")
        self.assertEqual(saved["ai_provider"], "OpenRouter")
        self.assertNotIn("sk-or-v1-", saved["details"])
        self.assertNotIn("Bearer EXAMPLE_TOKEN", saved["details"])
        self.assertIn("[REDACTED]", saved["details"])

    def test_outbox_migrates_legacy_secret_from_existing_payload(self):
        payload = {
            "event_id": "legacy-ai-secret",
            "ai_model": "sk-or-v1-THISISNOTAREALSECRET000000000000nvidia/model:free",
            "ai_provider": "OpenRouter",
            "ai_analysis": "legacy value",
            "details": "legacy text",
        }
        self.db.execute(
            """INSERT INTO csr_notion_event_outbox
               (event_id,payload_json,created_at_utc,attempts,next_attempt_at,sync_status)
               VALUES (?,?,?,?,?,?)""",
            ("legacy-ai-secret", __import__("json").dumps(payload), "2026-11-18T00:00:00Z", 0, 0, "PENDING"),
        )
        self.db.commit()
        NotionEventOutbox(self.db)
        row = self.db.execute(
            "SELECT payload_json FROM csr_notion_event_outbox WHERE event_id=?",
            ("legacy-ai-secret",),
        ).fetchone()
        saved = __import__("json").loads(row["payload_json"])
        self.assertEqual(saved["ai_model"], "")
        self.assertNotIn("sk-or-v1-", row["payload_json"])

    def test_notion_properties_store_provider_separately_from_safe_model(self):
        payload = self.outbox.normalize_event(event(
            event_id="cycle-ai:safe-model",
            event_type="AI_RECOMMENDATION",
            actor="Nemotron Supervisor",
            ai_provider="OpenRouter",
            ai_model="nvidia/nemotron-3-ultra-550b-a55b:free",
            ai_decision="OBSERVE_ONLY",
        ))
        properties = self.outbox._notion_properties(payload, 1)
        self.assertEqual(
            properties["Provider ناظر AI"]["rich_text"][0]["text"]["content"],
            "OpenRouter",
        )
        self.assertEqual(
            properties["مدل ناظر AI"]["rich_text"][0]["text"]["content"],
            "nvidia/nemotron-3-ultra-550b-a55b:free",
        )



    def test_get_event_survives_notion_sync_state_transition(self):
        self.outbox.enqueue(event(event_id="observer:REQUESTED"))
        pending = self.outbox.get_event("observer:REQUESTED")
        self.assertIsNotNone(pending)
        self.assertEqual(pending["event_id"], "observer:REQUESTED")
        self.assertEqual(pending["_outbox"]["sync_status"], "PENDING")

        with (
            patch(
                "crypto_super_radar.notion_event_sync.requests.post",
                return_value=FakeResponse({"results": [{"id": "notion-page-observer"}]}),
            ),
            patch(
                "crypto_super_radar.notion_event_sync.requests.patch",
                return_value=FakeResponse({"id": "notion-page-observer"}),
            ),
        ):
            self.outbox.flush_pending(now=1_800_000_000)
        synced = self.outbox.get_event("observer:REQUESTED")
        self.assertEqual(synced["_outbox"]["sync_status"], "SYNCED")
        self.assertEqual(synced["event_type"], "NO_TRADE")

    def test_status_snapshot_exposes_safe_retry_and_http_error_metadata(self):
        self.outbox.enqueue(event(event_id="backoff:429"))
        error = requests.HTTPError(
            "Notion unavailable",
            response=FakeResponse(status_code=429),
        )
        with patch(
            "crypto_super_radar.notion_event_sync.requests.post",
            side_effect=error,
        ):
            result = self.outbox.flush_pending(now=1_800_000_000)
        snapshot = self.outbox.status_snapshot()
        self.assertEqual(result["failed_this_run"], 1)
        self.assertEqual(snapshot["pending_with_errors"], 1)
        self.assertEqual(snapshot["pending_in_backoff"], 1)
        self.assertEqual(snapshot["pending_due_now"], 0)
        self.assertEqual(snapshot["oldest_pending_http_status"], 429)
        self.assertEqual(snapshot["oldest_error_http_status"], 429)
        self.assertEqual(snapshot["diagnostic_scope"], "SAFE_METADATA_ONLY")


if __name__ == "__main__":
    unittest.main()
