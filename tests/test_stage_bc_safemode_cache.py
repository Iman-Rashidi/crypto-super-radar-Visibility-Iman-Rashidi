"""Focused regression tests for Stage B/C safety and dashboard freshness."""
from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone

from crypto_super_radar.ai_supervisor import groq_decision_contract
from crypto_super_radar.challenge1_v2_autopilot import (
    _blofin_api_error_kind,
    run_cycle,
)
from crypto_super_radar.demo_dashboard_cache import (
    cached_demo_dashboard_report,
    cycle_lag_status,
)


class GroqDecisionContractTests(unittest.TestCase):
    def test_veto_and_reject_fail_closed_outside_demo_advisory(self):
        for status in ("VETO", "REJECT", "REJECTED"):
            with self.subTest(status=status):
                result = groq_decision_contract(
                    {"status": status}, demo_advisory_mode=False,
                    technical_approved=True, groq_approval_positive=True,
                )
                self.assertFalse(result["approved"])
                self.assertEqual(result["status"], "VETO")
                self.assertTrue(result["groq_vetoed"])
                self.assertTrue(result["entry_gate_enforced"])

    def test_demo_advisory_preserves_veto_as_telemetry(self):
        result = groq_decision_contract(
            {"status": "REJECT"}, demo_advisory_mode=True,
            technical_approved=True, groq_approval_positive=False,
        )
        self.assertTrue(result["approved"])
        self.assertEqual(result["status"], "APPROVED_WITH_GROQ_ADVISORY_VETO")
        self.assertTrue(result["groq_vetoed"])
        self.assertFalse(result["entry_gate_enforced"])

    def test_unverified_provider_cannot_be_labeled_as_groq_not_approved(self):
        result = groq_decision_contract(
            {"status": "APPROVED", "provider": "OpenRouter"},
            demo_advisory_mode=False, technical_approved=True,
            groq_approval_positive=False, verified_groq_review=False,
        )
        self.assertFalse(result["approved"])
        self.assertEqual(result["status"], "UNKNOWN")

    def test_demo_advisory_still_requires_technical_approval(self):
        result = groq_decision_contract(
            {"status": "APPROVED"}, demo_advisory_mode=True,
            technical_approved=False, groq_approval_positive=True,
        )
        self.assertFalse(result["approved"])


class BloFinSafeModeTests(unittest.TestCase):
    class Timeout(Exception):
        pass

    class HttpFailure(Exception):
        def __init__(self, status_code):
            super().__init__(f"HTTP {status_code}")
            self.status_code = status_code

    def test_only_timeout_429_and_5xx_are_countable(self):
        self.assertEqual(_blofin_api_error_kind(self.Timeout()), "TIMEOUT")
        self.assertEqual(_blofin_api_error_kind(self.HttpFailure(429)), "HTTP_429")
        self.assertEqual(_blofin_api_error_kind(self.HttpFailure(503)), "HTTP_503")
        self.assertIsNone(_blofin_api_error_kind(ValueError("bad market data")))
        self.assertIsNone(_blofin_api_error_kind(RuntimeError("NO_TRADE")))

    def test_third_consecutive_api_error_pauses_remaining_entries(self):
        db = sqlite3.connect(":memory:")
        try:
            calls = []
            def analyzer(_client, symbol):
                calls.append(symbol)
                raise self.Timeout("BloFin timed out")
            report = run_cycle(
                object(), db, analyzer=analyzer,
                symbols=("BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT"),
                environ={},
            )
            self.assertEqual(len(calls), 3)
            self.assertTrue(report["safe_mode_active"])
            self.assertEqual(report["api_error_count"], 3)
            self.assertEqual(report["symbols"][2]["status"], "SAFE_MODE_API_ERROR")
            self.assertEqual(report["symbols"][3]["status"], "SAFE_MODE_API_ERROR")
            saved = db.execute(
                "SELECT consecutive_failures FROM challenge1_v2_api_safety_state WHERE state_id=1"
            ).fetchone()
            self.assertEqual(saved[0], 3)
        finally:
            db.close()

    def test_no_trade_is_not_an_api_failure_and_success_resets_streak(self):
        db = sqlite3.connect(":memory:")
        try:
            first = run_cycle(
                object(), db, analyzer=lambda *_: (_ for _ in ()).throw(self.Timeout()),
                symbols=("BTC-USDT",), environ={},
            )
            self.assertEqual(first["api_error_count"], 1)
            second = run_cycle(
                object(), db,
                analyzer=lambda *_: {"signal": "NO_TRADE", "status": "NO_TRADE"},
                symbols=("BTC-USDT",), environ={},
            )
            self.assertEqual(second["api_error_count"], 0)
            self.assertFalse(second["safe_mode_active"])
            saved = db.execute(
                "SELECT consecutive_failures FROM challenge1_v2_api_safety_state WHERE state_id=1"
            ).fetchone()
            self.assertEqual(saved[0], 0)
        finally:
            db.close()


class DemoDashboardCacheTests(unittest.TestCase):
    def test_report_cache_expires_at_sixty_seconds(self):
        calls = []
        def loader():
            calls.append(len(calls) + 1)
            return {"closed_order_count": calls[-1]}
        first = cached_demo_dashboard_report(loader, ttl_seconds=60, now=1000.0)
        second = cached_demo_dashboard_report(loader, ttl_seconds=60, now=1059.0)
        third = cached_demo_dashboard_report(loader, ttl_seconds=60, now=1060.0)
        self.assertEqual(first["closed_order_count"], 1)
        self.assertEqual(second["closed_order_count"], 1)
        self.assertEqual(third["closed_order_count"], 2)
        self.assertEqual(len(calls), 2)

    def test_different_loader_functions_do_not_share_cached_reports(self):
        first = cached_demo_dashboard_report(lambda: {"source": "A"}, ttl_seconds=60, now=2000.0)
        second = cached_demo_dashboard_report(lambda: {"source": "B"}, ttl_seconds=60, now=2001.0)
        self.assertEqual(first["source"], "A")
        self.assertEqual(second["source"], "B")

    def test_cycle_lag_warning_only_after_twenty_five_minutes(self):
        now = datetime(2026, 11, 26, 12, 30, tzinfo=timezone.utc)
        current = cycle_lag_status("2026-11-26T12:05:00+00:00", now=now)
        delayed = cycle_lag_status("2026-11-26T12:04:59+00:00", now=now)
        self.assertFalse(current["cycle_lag_warning"])
        self.assertEqual(current["cycle_lag_status"], "CURRENT")
        self.assertTrue(delayed["cycle_lag_warning"])
        self.assertEqual(delayed["cycle_lag_status"], "DELAYED")

    def test_missing_cycle_timestamp_is_explicitly_stale(self):
        result = cycle_lag_status(None)
        self.assertTrue(result["cycle_lag_warning"])
        self.assertEqual(result["cycle_lag_status"], "NO_SUCCESSFUL_CYCLE")


if __name__ == "__main__":
    unittest.main()
