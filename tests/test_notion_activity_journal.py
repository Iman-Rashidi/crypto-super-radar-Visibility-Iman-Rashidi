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
        "candles_15m": candles(40),
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
               SET attempts=2, last_error_type='HTTPError', last_http_status=403
               WHERE experience_id=?""",
            (snapshot["experience_id"],),
        )
        self.connection.commit()
        state = self.journal.status_snapshot()
        self.assertEqual(state["pending"], 1)
        self.assertEqual(state["synced"], 0)
        self.assertEqual(state["failed"], 0)
        self.assertEqual(state["latest_experience_id"], snapshot["experience_id"])
        self.assertEqual(state["latest_attempts"], 2)
        self.assertEqual(state["last_error_type"], "HTTPError")
        self.assertEqual(state["last_http_status"], 403)

    def test_chart_is_a_png_rendered_from_real_ohlcv_and_exit_levels(self):
        snapshot = sample_snapshot()
        snapshot.update({
            "actual_exit_price": 112.0,
            "closed_confirmed": True,
        })
        rendered = render_candlestick_chart(snapshot)
        self.assertTrue(rendered.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertGreater(len(rendered), 5000)

    def test_csv_contains_both_real_market_timeframes(self):
        payload = render_ohlcv_csv(sample_snapshot()).decode("utf-8")
        self.assertIn("15m,", payload)
        self.assertIn("1H,", payload)
        self.assertIn("open,high,low,close,volume", payload)


if __name__ == "__main__":
    unittest.main()
