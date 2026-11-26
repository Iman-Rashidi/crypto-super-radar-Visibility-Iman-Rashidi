from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from crypto_super_radar.ai_supervisor import run_challenge1_v2_supervisor_reviews
from crypto_super_radar.blofin_demo_executor import _challenge1_two_reviewer_approval_ready


def _analysis(phase="WAVE_3_CONFIRMED", pattern="BULLISH"):
    return SimpleNamespace(
        signal=SimpleNamespace(value="LONG"),
        metrics={
            "execution_profile": "CHALLENGE_1_V2",
            "challenge1_v2_evidence": {
                "elliott_wave": {
                    "phase": phase,
                    "pattern": pattern,
                }
            },
        },
    )


def _groq_review(status="APPROVED", **overrides):
    review = {
        "provider": "Groq",
        "model": "openai/gpt-oss-20b",
        "returned_model": "openai/gpt-oss-20b",
        "status": status,
        "paper_entry_eligible": status == "APPROVED",
        "request_attempted": True,
        "response_source": "LIVE_API",
        "response_validated": True,
        "model_verified": True,
        "expected_side": "LONG",
        "trader_proposal": "LONG",
        "elliott_wave_phase": "WAVE_3_CONFIRMED",
        "elliott_wave_direction": "BULLISH",
    }
    review.update(overrides)
    return review


class DemoAdvisorySupervisorTests(unittest.TestCase):
    def _run(self, review, live="false", analysis=None):
        env = {
            "CHALLENGE_1_V2_EASY_DEMO_ENTRY_MODE": "true",
            "BLOFIN_DEMO_TRADING_ENABLED": "true",
            "BLOFIN_LIVE_ENABLED": live,
        }
        with patch.dict(os.environ, env, clear=False):
            return run_challenge1_v2_supervisor_reviews(
                analysis or _analysis(), "BTC-USDT", signal_id="signal-demo-001",
                second_reviewer=lambda *args, **kwargs: review,
            )

    def test_demo_entry_passes_groq_veto_but_records_veto(self):
        result = self._run(_groq_review("VETO"))
        self.assertTrue(result["approved"])
        self.assertEqual(result["status"], "APPROVED_WITH_GROQ_ADVISORY_VETO")
        consensus = result["ai_consensus"]
        self.assertTrue(consensus["reviewer_approved"])
        self.assertTrue(consensus["technical_approved"])
        self.assertTrue(consensus["demo_advisory_mode"])
        self.assertFalse(consensus["groq_approval_positive"])
        self.assertTrue(consensus["groq_vetoed"])
        self.assertEqual(consensus["verified_approval_count"], 0)

    def test_demo_entry_passes_groq_429_failure_as_advisory_veto(self):
        result = self._run(_groq_review(
            "ERROR", http_status=429, request_attempted=True,
            response_source="UNKNOWN", response_validated=False,
            model_verified=False, paper_entry_eligible=False,
        ))
        self.assertTrue(result["approved"])
        self.assertTrue(result["ai_consensus"]["groq_vetoed"])
        self.assertFalse(result["ai_consensus"]["groq_approval_positive"])

    def test_live_flag_disables_demo_advisory_override(self):
        result = self._run(_groq_review("VETO"), live="true")
        self.assertFalse(result["approved"])
        self.assertFalse(result["ai_consensus"]["demo_advisory_mode"])
        self.assertTrue(result["ai_consensus"]["groq_vetoed"])


class DemoAdvisoryExecutorTests(unittest.TestCase):
    def _record(self):
        now = datetime.now(timezone.utc).isoformat()
        signal_id = "signal-demo-001"
        return {
            "execution_profile": "CHALLENGE_1_V2",
            "signal": "LONG",
            "signal_id": signal_id,
            "challenge1_v2_evidence": {
                "signal_id": signal_id,
                "elliott_wave": {"phase": "WAVE_3_CONFIRMED", "pattern": "BULLISH"},
            },
            "ai_consensus": {
                "status": "VETO",
                "reviewer_status": "VETO",
                "reviewer_approved": True,
                "technical_approved": True,
                "demo_advisory_mode": True,
                "groq_vetoed": True,
                "groq_approval_positive": False,
                "expected_side": "LONG",
                "reviewed_signal_id": signal_id,
                "reviewed_at_utc": now,
            },
            "ai_reviews": [{"reviewer_id": "groq", "review": {"status": "VETO"}}],
        }

    def test_executor_ignores_individual_groq_veto_only_in_demo_advisory_mode(self):
        env = {
            "CHALLENGE_1_V2_EASY_DEMO_ENTRY_MODE": "true",
            "BLOFIN_DEMO_TRADING_ENABLED": "true",
            "BLOFIN_LIVE_ENABLED": "false",
        }
        with patch.dict(os.environ, env, clear=False), \
             patch("crypto_super_radar.blofin_demo_executor.challenge1_v2_execution_evidence_ready",
                   return_value=(True, "valid")), \
             patch("crypto_super_radar.blofin_demo_executor._v2_elliott_entry_gate_allows",
                   return_value=True):
            ready, reason = _challenge1_two_reviewer_approval_ready(self._record())
        self.assertTrue(ready, reason)

    def test_executor_rejects_signal_id_mismatch_in_advisory_mode(self):
        record = self._record()
        record["ai_consensus"]["reviewed_signal_id"] = "different-signal"
        env = {
            "CHALLENGE_1_V2_EASY_DEMO_ENTRY_MODE": "true",
            "BLOFIN_DEMO_TRADING_ENABLED": "true",
            "BLOFIN_LIVE_ENABLED": "false",
        }
        with patch.dict(os.environ, env, clear=False), \
             patch("crypto_super_radar.blofin_demo_executor.challenge1_v2_execution_evidence_ready",
                   return_value=(True, "valid")), \
             patch("crypto_super_radar.blofin_demo_executor._v2_elliott_entry_gate_allows",
                   return_value=True):
            ready, reason = _challenge1_two_reviewer_approval_ready(record)
        self.assertFalse(ready)
        self.assertIn("signal_id", reason)

    def test_executor_never_uses_advisory_override_when_live_flag_is_true(self):
        env = {
            "CHALLENGE_1_V2_EASY_DEMO_ENTRY_MODE": "true",
            "BLOFIN_DEMO_TRADING_ENABLED": "true",
            "BLOFIN_LIVE_ENABLED": "true",
        }
        with patch.dict(os.environ, env, clear=False), \
             patch("crypto_super_radar.blofin_demo_executor.challenge1_v2_execution_evidence_ready",
                   return_value=(True, "valid")), \
             patch("crypto_super_radar.blofin_demo_executor._v2_elliott_entry_gate_allows",
                   return_value=True):
            ready, _ = _challenge1_two_reviewer_approval_ready(self._record())
        self.assertFalse(ready)


if __name__ == "__main__":
    unittest.main()
