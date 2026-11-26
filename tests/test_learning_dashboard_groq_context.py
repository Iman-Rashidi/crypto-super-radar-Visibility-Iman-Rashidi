"""Tests for Groq historical context injection and dashboard learning panel."""
import unittest
from types import SimpleNamespace

from crypto_super_radar.ai_supervisor import _groq_historical_review_role
from crypto_super_radar.web_app import (
    _learning_lessons_panel_html,
    _learning_lessons_payload_from_experiences,
)


def experience(symbol="BTC-USDT", result="PROFIT", pnl=1.5, code="LATE_WAVE_ENTRY"):
    return {
        "verification": {"status": "PASS"},
        "events": [
            {"event_type": "DECISION_RECORDED", "payload": {"symbol": symbol}},
            {"event_type": "OUTCOME_LABELLED", "payload": {
                "final": True, "realized_result": result, "realized_net_pnl": pnl,
                "evidence": {"post_trade_autopsy": {"diagnosis_code": code}},
            }},
        ],
    }


class GroqHistoricalContextTests(unittest.TestCase):
    def test_groq_role_contains_only_compact_symbol_history(self):
        role = _groq_historical_review_role(
            SimpleNamespace(raw="perp:BTC-USDT"),
            [experience(), experience(result="LOSS", pnl=-0.4)],
        )
        self.assertIn("HIST BTC-USDT", role)
        self.assertIn("N=2", role)
        self.assertIn("W=50.0%", role)
        self.assertIn("TOP=LATE_WAVE_ENTRY", role)
        self.assertLessEqual(len(role), 80)

    def test_groq_role_falls_back_when_no_verified_history(self):
        role = _groq_historical_review_role("ETH-USDT", [])
        self.assertEqual(role, "SOLE AI SUPERVISOR")


class LearningDashboardTests(unittest.TestCase):
    def test_dashboard_payload_shows_persian_lesson_and_autopsy(self):
        exp = experience()
        exp["events"].append({
            "event_type": "LESSON_PROPOSED",
            "payload": {"lesson_id": "L-1", "lesson": "ورود در انتهای موج را محدود کن."},
        })
        payload = _learning_lessons_payload_from_experiences([exp])
        self.assertEqual(payload["status"], "OK")
        self.assertIn("ورود در انتهای موج", payload["lessons"][0]["lesson"])
        self.assertEqual(payload["outcomes"][0]["result_fa"], "برد")
        self.assertIn("بخش پایانی موج", payload["outcomes"][0]["reason_fa"])

    def test_dashboard_empty_state_and_html_panel(self):
        payload = _learning_lessons_payload_from_experiences([])
        self.assertEqual(payload["status"], "EMPTY")
        self.assertIn("هنوز تجربه", payload["message_fa"])
        markup = _learning_lessons_panel_html()
        self.assertIn("درس‌های آموخته‌شده و کالبدشکافی معاملات ربات", markup)
        self.assertIn("/api/learning/lessons", markup)


if __name__ == "__main__":
    unittest.main()
