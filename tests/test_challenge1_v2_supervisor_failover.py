from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from crypto_super_radar.ai_supervisor import run_challenge1_v2_supervisor_reviews


def valid_approval():
    return {
        "status": "APPROVED",
        "paper_entry_eligible": True,
        "request_attempted": True,
        "response_source": "LIVE_API",
        "response_validated": True,
        "model_verified": True,
        "model": "verified-model-a",
        "returned_model": "verified-model-a",
        "provider": "provider-a",
        "expected_side": "LONG",
        "trader_proposal": "LONG",
        "elliott_wave_phase": "WAVE_3_CONFIRMED",
        "elliott_wave_direction": "BULLISH",
    }


def analysis():
    return SimpleNamespace(
        signal=SimpleNamespace(value="LONG"),
        metrics={
            "execution_profile": "CHALLENGE_1_V2",
            "challenge1_v2_evidence": {
                "elliott_wave": {
                    "phase": "WAVE_3_CONFIRMED",
                    "pattern": "BULLISH",
                }
            },
        },
    )


class Challenge1V2DemoReviewerFailoverTests(unittest.TestCase):
    def invoke(self, second_review, *, enabled=True):
        env = {
            "CHALLENGE_1_V2_DEMO_AUTOPILOT_ENABLED": "true" if enabled else "false",
            "BLOFIN_DEMO_TRADING_ENABLED": "true" if enabled else "false",
        }
        with patch.dict(os.environ, env, clear=True):
            return run_challenge1_v2_supervisor_reviews(
                analysis(), "perp:BTCUSDT",
                primary_reviewer=lambda *_args, **_kwargs: valid_approval(),
                second_reviewer=lambda *_args, **_kwargs: second_review,
            )

    def test_one_verified_approval_plus_http_502_allows_demo_only_failover(self):
        result = self.invoke({"status": "UNKNOWN", "http_status": 502})
        self.assertTrue(result["approved"])
        self.assertEqual(result["status"], "APPROVED_WITH_TRANSIENT_PROVIDER_FAILURE")
        self.assertTrue(result["ai_consensus"]["single_reviewer_demo_failover"])
        self.assertEqual(result["ai_consensus"]["verified_approval_count"], 1)
        self.assertEqual(result["ai_consensus"]["transient_provider_failure_count"], 1)

    def test_timeout_exception_is_a_transient_failure(self):
        result = self.invoke({"status": "ERROR", "error_type": "ReadTimeout"})
        self.assertTrue(result["approved"])
        self.assertEqual(result["ai_consensus"]["status"], "APPROVED_WITH_TRANSIENT_PROVIDER_FAILURE")

    def test_failover_is_disabled_outside_explicit_demo_autopilot(self):
        result = self.invoke({"status": "UNKNOWN", "http_status": 502}, enabled=False)
        self.assertFalse(result["approved"])
        self.assertNotEqual(result["ai_consensus"]["status"], "APPROVED_WITH_TRANSIENT_PROVIDER_FAILURE")

    def test_explicit_veto_always_overrides_other_reviewer_approval(self):
        result = self.invoke({"status": "VETO", "paper_entry_eligible": False})
        self.assertFalse(result["approved"])
        self.assertEqual(result["ai_consensus"]["status"], "VETO")
        self.assertFalse(result["ai_consensus"]["single_reviewer_demo_failover"])

    def test_malformed_or_unconfigured_review_does_not_trigger_failover(self):
        for failure in (
            {"status": "UNKNOWN", "error_type": "ValueError"},
            {"status": "NOT_CONFIGURED", "error_type": "MissingAPIKey"},
            {"status": "RATE_LIMITED", "http_status": 429},
        ):
            with self.subTest(failure=failure):
                result = self.invoke(failure)
                self.assertFalse(result["approved"])
                self.assertNotEqual(result["ai_consensus"]["status"], "APPROVED_WITH_TRANSIENT_PROVIDER_FAILURE")


if __name__ == "__main__":
    unittest.main()
