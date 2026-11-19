from __future__ import annotations

import sqlite3
import unittest

from crypto_super_radar.notion_activity_journal import (
    NotionActivityJournal,
    render_candlestick_chart,
    render_ohlcv_csv,
)


def candles(count: int, *, base: float = 100.0):
    rows = []
    for index in range(count):
        opened = base + index * 0.2
        close = opened + (0.15 if index % 2 == 0 else -0.1)
        rows.append({
            "time_utc": f"2026-11-17T{16 + (index * 5) // 60:02d}:{(index * 5) % 60:02d}:00Z",
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
        "reason_fa": "Bullish Engulfing pattern on a completed 5m candle.",
        "analysis_time_utc": "2026-11-17T16:05:00Z",
        "signal_bar_open_utc": "2026-11-17T16:00:00Z",
        "engine_version": "CHALLENGE_1_LOCAL_RULES_V4_5M_ONLY",
        "patterns": [{"name_en": "Bullish Engulfing", "direction": "BULLISH"}],
        "volume_confirmation": {"confirmed": False, "volume_ratio": 0.8},
        "evidence": {"completed_candles_only": True},
        "candles_5m": candles(40),
        "reviewer_consensus": "AGREEMENT",
        "ai_consensus": {
            "status": "AGREEMENT",
            "primary_verified": True,
            "second_verified": True,
            "two_reviewer_approval": True,
        },
        "ai_reviews": [
            {
                "reviewer_id": "nemotron",
                "actor": "Nemotron Supervisor",
                "status": "APPROVED",
                "provider": "OpenRouter",
                "model": "test-nemotron",
                "returned_model": "test-nemotron",
                "request_attempted": True,
                "response_source": "LIVE_API",
                "response_validated": True,
                "model_verified": True,
                "paper_entry_eligible": True,
                "http_status": 200,
                "latency_ms": 120,
                "reason": "Pattern evidence checked",
            },
            {
                "reviewer_id": "second_ai",
                "actor": "Second AI Supervisor",
                "status": "APPROVED",
                "provider": "Groq",
                "model": "test-second",
                "returned_model": "test-second",
                "request_attempted": True,
                "response_source": "LIVE_API",
                "response_validated": True,
                "model_verified": True,
                "paper_entry_eligible": True,
                "http_status": 200,
                "latency_ms": 150,
                "reason": "Independent pattern review",
            },
        ],
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

    def test_legacy_notion_token_alias_is_accepted(self):
        from unittest.mock import patch

        with patch.dict("os.environ", {"NOTION_API_TOKEN": "", "NOTION_TOKEN": "legacy-test-secret"}):
            state = self.journal.status_snapshot()
            self.assertEqual(state["status"], "CONFIGURED")
            with patch("crypto_super_radar.notion_activity_journal.requests.post") as post:
                result = self.journal.flush_pending(now=1_800_000_000)
        self.assertEqual(result["status"], "CONFIGURED")
        post.assert_not_called()

    def test_enqueue_is_idempotent_and_updates_same_experience(self):
        snapshot = sample_snapshot()
        self.assertTrue(self.journal.enqueue(snapshot))
        saved = self.journal.get_snapshot(snapshot["experience_id"])
        self.assertEqual(saved["symbol"], "BTC-USDT")
        self.assertEqual(len(saved["candles_5m"]), 40)
        self.assertNotIn("candles_15m", saved)
        self.assertNotIn("candles_1h", saved)
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
               SET attempts=2, last_error_type='HTTPError', last_http_status=403, last_error_stage='POST /data_sources/{activity}/query'
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
        self.assertEqual(state["last_http_status"], 403)
        self.assertEqual(state["last_error_stage"], "POST /data_sources/{activity}/query")

    def test_chart_is_a_png_rendered_from_real_ohlcv_and_exit_levels(self):
        snapshot = sample_snapshot()
        snapshot.update({
            "actual_exit_price": 112.0,
            "closed_confirmed": True,
        })
        rendered = render_candlestick_chart(snapshot)
        self.assertTrue(rendered.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertGreater(len(rendered), 5000)

    def test_csv_contains_only_real_completed_5m_market_candles(self):
        payload = render_ohlcv_csv(sample_snapshot()).decode("utf-8")
        self.assertIn("5m,", payload)
        self.assertNotIn("15m,", payload)
        self.assertNotIn("1H,", payload)
        self.assertIn("open,high,low,close,volume", payload)

    def test_notion_properties_report_5m_only_and_verified_independent_review(self):
        snapshot = sample_snapshot()
        self.journal.enqueue(snapshot)
        saved = self.journal.get_snapshot(snapshot["experience_id"])
        properties = self.journal._properties(
            saved, chart_upload_id=None, ohlcv_upload_id=None,
        )
        self.assertEqual(properties["تایم‌فریم اجرا"]["select"]["name"], "5m")
        self.assertIsNone(properties["تایم‌فریم زمینه"]["select"])
        self.assertEqual(properties["بازبینی مستقل"]["select"]["name"], "تأیید")
        evidence_text = properties["گزارش شواهد"]["rich_text"][0]["text"]["content"]
        self.assertIn("Nemotron", evidence_text)
        self.assertIn("second_ai", evidence_text)

    def test_missing_or_unverified_ai_supervisor_is_not_reported_as_approved(self):
        snapshot = sample_snapshot()
        snapshot["reviewer_consensus"] = "NOT_COMPARABLE"
        snapshot["ai_consensus"] = {"status": "NOT_COMPARABLE"}
        snapshot["ai_reviews"][1].update({
            "status": "NOT_CONFIGURED",
            "request_attempted": False,
            "response_source": "NONE",
            "response_validated": False,
            "model_verified": False,
            "returned_model": "",
            "paper_entry_eligible": False,
        })
        self.journal.enqueue(snapshot)
        saved = self.journal.get_snapshot(snapshot["experience_id"])
        self.assertEqual(self.journal._independent_review_status(saved), "نیازمند بازبینی")

    def test_sync_skips_chart_upload_when_fewer_than_five_real_5m_candles_exist(self):
        snapshot = sample_snapshot()
        snapshot["candles_5m"] = candles(4)
        calls = []
        def fake_request(method, url, token, *, body=None):
            calls.append((method, url, body))
            return {"id": "notion-page-1"}
        from unittest.mock import patch
        with patch.object(self.journal, "_request_json", side_effect=fake_request):
            page_id = self.journal._sync_row("not-a-real-token", snapshot, None)
        self.assertEqual(page_id, "notion-page-1")
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0][1].endswith("/pages"))


if __name__ == "__main__":
    unittest.main()
