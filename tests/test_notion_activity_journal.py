from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch
import requests

from crypto_super_radar.notion_activity_journal import (
    NotionActivityJournal,
    _notion_http_error_details,
    render_candlestick_chart,
    render_ohlcv_csv,
)


def candles(count: int, *, base: float = 100.0):
    rows = []
    for index in range(count):
        opened = base + index * 0.2
        close = opened + (0.15 if index % 2 == 0 else -0.1)
        rows.append({
            "time_utc": f"2026-11-17T{index // 4:02d}:{(index % 4) * 15:02d}:00Z",
            "open": opened,
            "high": max(opened, close) + 0.2,
            "low": min(opened, close) - 0.2,
            "close": close,
            "volume": 1000 + index * 10,
            "quote_volume": 100000 + index * 100,
        })
    return rows


def sample_snapshot():
    return {
        "experience_id": "blofin-ch1-test-experience-001",
        "job_id": "job-001",
        "symbol": "BTC-USDT",
        "signal": "LONG",
        "status": "FILLED",
        "reason_fa": "Bullish Engulfing pattern on a completed 15m candle.",
        "analysis_time_utc": "2026-11-17T16:15:00Z",
        "signal_bar_open_utc": "2026-11-17T16:00:00Z",
        "engine_version": "CHALLENGE_1_LOCAL_RULES_V2",
        "patterns": [{"name_en": "Bullish Engulfing", "direction": "BULLISH"}],
        "volume_confirmation": {"confirmed": False, "volume_ratio": 0.8},
        "evidence": {"completed_candles_only": True},
        "candles_5m": candles(40),
        "candles_15m": candles(40, base=95.0),
        "candles_1h": candles(20, base=90.0),
        "entry_price": 108.0,
        "stop_loss": 105.0,
        "take_profit": 114.0,
        "actual_entry_price": 108.1,
        "actual_entry_time_utc": "2026-11-17T16:16:00Z",
        "order_id": "blofin-order-001",
        "position_id": "blofin-position-001",
        "trade_executed": True,
        "orders_submitted": True,
    }


class NotionActivityJournalTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.journal = NotionActivityJournal(self.connection)

    def tearDown(self):
        self.connection.close()

    def test_enqueue_is_idempotent_and_updates_same_experience(self):
        snapshot = sample_snapshot()
        self.assertTrue(self.journal.enqueue(snapshot))
        saved = self.journal.get_snapshot(snapshot["experience_id"])
        self.assertEqual(saved["symbol"], "BTC-USDT")
        self.assertEqual(len(saved["candles_5m"]), 40)
        self.assertEqual(len(saved["candles_15m"]), 40)
        changed = dict(snapshot)
        changed["status"] = "CLOSED_RECONCILED"
        changed["closed_confirmed"] = True
        changed["actual_exit_price"] = 112.0
        self.journal.enqueue(changed)
        saved = self.journal.get_snapshot(snapshot["experience_id"])
        self.assertEqual(saved["status"], "CLOSED_RECONCILED")
        self.assertTrue(saved["closed_confirmed"])
        self.assertEqual(self.journal.pending_count(), 1)

    def test_status_snapshot_exposes_retry_and_http_error_details(self):
        snapshot = sample_snapshot()
        self.journal.enqueue(snapshot)
        self.connection.execute(
            """UPDATE csr_notion_activity_journal
               SET attempts=2, last_error_type='HTTPError', last_http_status=404,
                   last_error_code='object_not_found',
                   last_error_message='Could not find data source. Check whether the database is shared with this integration.',
                   last_error_stage='POST /data_sources/{activity}/query'
               WHERE experience_id=?""",
            (snapshot["experience_id"],),
        )
        self.connection.commit()
        newer = dict(sample_snapshot(), experience_id="newer-pending-experience-002", symbol="ETH-USDT")
        self.journal.enqueue(newer)
        state = self.journal.status_snapshot()
        self.assertEqual(state["pending"], 2)
        self.assertEqual(state["synced"], 0)
        self.assertEqual(state["failed"], 1)
        self.assertEqual(state["pending_with_error"], 1)
        self.assertEqual(state["latest_experience_id"], newer["experience_id"])
        self.assertEqual(state["latest_attempts"], 0)
        self.assertEqual(state["last_failed_experience_id"], snapshot["experience_id"])
        self.assertEqual(state["last_attempts"], 2)
        self.assertEqual(state["last_error_type"], "HTTPError")
        self.assertEqual(state["last_http_status"], 404)
        self.assertEqual(state["last_error_code"], "object_not_found")
        self.assertIn("shared with this integration", state["last_error_message"])
        self.assertEqual(state["last_error_stage"], "POST /data_sources/{activity}/query")

    def test_notion_http_error_details_extracts_code_and_message_without_headers(self):
        class FakeResponse:
            status_code = 404

            def json(self):
                return {
                    "object": "error",
                    "status": 404,
                    "code": "object_not_found",
                    "message": "Could not find database. Please make sure it is shared with your integration.",
                }

        error = requests.HTTPError("404 Client Error")
        error.response = FakeResponse()
        code, message = _notion_http_error_details(error)
        self.assertEqual(code, "object_not_found")
        self.assertIn("shared with your integration", message)
        self.assertNotIn("Authorization", message)

    def test_independent_review_status_and_position_id_are_written_to_activity_properties(self):
        snapshot = sample_snapshot()
        snapshot.update({
            "reviewer_consensus": "AGREEMENT",
            "ai_status": "APPROVED_BY_BOTH",
            "ai_consensus": {
                "status": "AGREEMENT", "primary_verified": True,
                "second_verified": True, "two_reviewer_approval": True,
                "decision": "APPROVE",
            },
            "ai_reviews": [
                {
                    "reviewer_id": "nemotron", "actor": "Nemotron Supervisor",
                    "status": "APPROVED", "provider": "OpenRouter",
                    "model": "model-a", "returned_model": "model-a",
                    "request_attempted": True, "response_source": "LIVE_API",
                    "response_validated": True, "model_verified": True,
                    "paper_entry_eligible": True, "reason": "approved",
                },
                {
                    "reviewer_id": "second_ai", "actor": "Second AI Supervisor",
                    "status": "APPROVED", "provider": "Groq",
                    "model": "openai/gpt-oss-120b", "returned_model": "openai/gpt-oss-120b",
                    "request_attempted": True, "response_source": "LIVE_API",
                    "response_validated": True, "model_verified": True,
                    "paper_entry_eligible": True, "reason": "approved",
                },
            ],
        })
        saved = __import__(
            "crypto_super_radar.notion_activity_journal",
            fromlist=["_normalise_snapshot"],
        )._normalise_snapshot(snapshot)
        properties = self.journal._properties(
            saved, chart_upload_id="chart-upload-id", ohlcv_upload_id="csv-upload-id",
        )
        self.assertEqual(properties["بازبینی مستقل"]["select"]["name"], "تأیید")
        self.assertIn("blofin-position-001", properties["گزارش شواهد"]["rich_text"][0]["text"]["content"])
        self.assertIn("second_ai", properties["گزارش شواهد"]["rich_text"][0]["text"]["content"])
        self.assertIn("تصویر چارت", properties)
        self.assertIn("داده OHLCV", properties)

    def test_rejected_verified_reviewer_sets_rejected_activity_status(self):
        snapshot = sample_snapshot()
        snapshot.update({
            "reviewer_consensus": "DISAGREEMENT",
            "ai_reviews": [
                {
                    "reviewer_id": "nemotron", "status": "VETO",
                    "request_attempted": True, "response_source": "LIVE_API",
                    "response_validated": True, "model_verified": True,
                },
            ],
        })
        saved = __import__(
            "crypto_super_radar.notion_activity_journal",
            fromlist=["_normalise_snapshot"],
        )._normalise_snapshot(snapshot)
        properties = self.journal._properties(saved, chart_upload_id=None, ohlcv_upload_id=None)
        self.assertEqual(properties["بازبینی مستقل"]["select"]["name"], "رد")

    def test_chart_is_a_png_rendered_from_real_ohlcv_and_exit_levels(self):
        snapshot = sample_snapshot()
        snapshot.update({
            "actual_exit_price": 112.0,
            "closed_confirmed": True,
        })
        rendered = render_candlestick_chart(snapshot)
        self.assertTrue(rendered.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertGreater(len(rendered), 5000)

    def test_sync_row_uploads_real_png_and_csv_and_attaches_both_to_same_notion_page(self):
        snapshot = sample_snapshot()
        request_calls = []
        upload_calls = []

        def fake_request(method, url, token, *, body=None):
            request_calls.append((method, url, body))
            if url.endswith("/file_uploads"):
                return {"id": f"upload-session-{len(request_calls)}"}
            if url.endswith("/pages"):
                return {"id": "notion-page-123"}
            raise AssertionError(f"Unexpected Notion API request: {method} {url}")

        class UploadedFileResponse:
            def __init__(self, file_id):
                self.file_id = file_id

            def raise_for_status(self):
                return None

            def json(self):
                return {"id": self.file_id}

        def fake_send(url, *, headers, files, timeout):
            filename, payload, content_type = files["file"]
            upload_calls.append({
                "url": url, "filename": filename,
                "payload": payload, "content_type": content_type,
                "timeout": timeout,
            })
            return UploadedFileResponse(
                "notion-chart-file" if content_type == "image/png" else "notion-ohlcv-file"
            )

        with patch.object(self.journal, "_request_json", side_effect=fake_request), patch(
            "crypto_super_radar.notion_activity_journal.requests.post",
            side_effect=fake_send,
        ):
            page_id = self.journal._sync_row("notion-test-token", snapshot, None)

        self.assertEqual(page_id, "notion-page-123")
        self.assertEqual(len(upload_calls), 2)
        chart_upload = next(item for item in upload_calls if item["content_type"] == "image/png")
        csv_upload = next(item for item in upload_calls if item["content_type"] == "text/csv")
        self.assertEqual(chart_upload["payload"][:8], bytes([137, 80, 78, 71, 13, 10, 26, 10]))
        self.assertIn(b"5m,", csv_upload["payload"])
        self.assertIn(b"15m,", csv_upload["payload"])
        self.assertIn(b"1H,", csv_upload["payload"])
        page_request = next(
            item for item in request_calls
            if item[1].endswith("/pages")
        )
        properties = page_request[2]["properties"]
        self.assertEqual(
            properties["تصویر چارت"]["files"][0]["file_upload"]["id"],
            "notion-chart-file",
        )
        self.assertEqual(
            properties["داده OHLCV"]["files"][0]["file_upload"]["id"],
            "notion-ohlcv-file",
        )

    def test_sync_row_does_not_attach_fabricated_chart_when_market_history_is_too_short(self):
        snapshot = sample_snapshot()
        snapshot["candles_5m"] = candles(4)
        request_calls = []

        def fake_request(method, url, token, *, body=None):
            request_calls.append((method, url, body))
            if url.endswith("/pages"):
                return {"id": "notion-page-short-history"}
            raise AssertionError("Short market history must not create a file upload.")

        with patch.object(self.journal, "_request_json", side_effect=fake_request):
            page_id = self.journal._sync_row("notion-test-token", snapshot, None)

        self.assertEqual(page_id, "notion-page-short-history")
        self.assertEqual(len(request_calls), 1)
        properties = request_calls[0][2]["properties"]
        self.assertNotIn("تصویر چارت", properties)
        self.assertNotIn("داده OHLCV", properties)

    def test_csv_contains_both_real_market_timeframes(self):
        payload = render_ohlcv_csv(sample_snapshot()).decode("utf-8")
        self.assertIn("5m,", payload)
        self.assertIn("15m,", payload)
        self.assertIn("1H,", payload)
        self.assertIn("open,high,low,close,volume", payload)


if __name__ == "__main__":
    unittest.main()
