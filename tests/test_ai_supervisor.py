from __future__ import annotations

import json
import os
import threading
import unittest
from unittest.mock import patch

from crypto_super_radar.ai_supervisor import (
    _message_text_candidates, ai_supervisor_status, run_ai_supervisor,
)
from crypto_super_radar.ai_second_opinion import DEFAULT_FREE_MODEL
from test_ai_second_opinion import valid_result, valid_document


class MultiProviderAIApproval:
    def __init__(self, provider_model: str, document: dict, status_code: int = 200):
        self.status_code = status_code
        self.provider_model = provider_model
        self.document = document

    def json(self):
        return {
            "model": self.provider_model,
            "usage": {"total_tokens": 75},
            "choices": [{"message": {"content": json.dumps(self.document, ensure_ascii=False)}}],
        }


class MultiProviderSupervisorTests(unittest.TestCase):
    def setUp(self):
        from crypto_super_radar import ai_supervisor
        with ai_supervisor._REQUEST_LOCK:
            ai_supervisor._DAILY_CALLS.clear()
            ai_supervisor._MINUTE_CALLS.clear()

    def test_extracts_json_split_across_structured_content_text_parts(self):
        document = valid_document()
        serialized = json.dumps(document, ensure_ascii=False)
        split_at = serialized.index('"final_rating"')
        candidates = _message_text_candidates({
            "content": [
                {"type": "text", "text": serialized[:split_at]},
                {"type": "text", "text": serialized[split_at:]},
                {"type": "image", "image_url": {"url": "ignored"}},
            ],
            "reasoning": "This must not replace valid final content.",
        })
        self.assertTrue(candidates)
        from crypto_super_radar.ai_second_opinion import _extract_json
        self.assertEqual(_extract_json(candidates[0]), document)

    def test_uses_reasoning_json_only_when_final_content_is_empty(self):
        document = valid_document()
        candidates = _message_text_candidates({
            "content": None,
            "reasoning": json.dumps(document, ensure_ascii=False),
        })
        self.assertTrue(candidates)
        from crypto_super_radar.ai_second_opinion import _extract_json
        self.assertEqual(_extract_json(candidates[0]), document)

    def test_ignores_non_text_content_parts_and_plain_reasoning(self):
        candidates = _message_text_candidates({
            "content": [{"type": "image", "image_url": {"url": "ignored"}}],
            "reasoning": "I considered the market but have no final JSON.",
        })
        from crypto_super_radar.ai_second_opinion import _extract_json
        self.assertTrue(candidates)
        with self.assertRaises(ValueError):
            _extract_json(candidates[-1])

    def test_status_counts_configured_providers_without_exposing_secrets(self):
        with patch.dict(os.environ, {
            "OPENROUTER_API_KEY": "openrouter-secret",
            "GEMINI_API_KEY": "gemini-secret",
        }, clear=True):
            status = ai_supervisor_status()
        self.assertEqual(status["configured_provider_count"], 2)
        self.assertTrue(status["configured"])
        self.assertEqual(status["status"], "READY")
        rendered = json.dumps(status)
        self.assertNotIn("openrouter-secret", rendered)
        self.assertNotIn("gemini-secret", rendered)

    def test_one_provider_never_has_authority_to_approve(self):
        calls = []
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "one-secret"}, clear=True):
            result = run_ai_supervisor(
                valid_result(), "perp:BTCUSDT",
                http_post=lambda *args, **kwargs: calls.append(args) or None,
            )
        self.assertEqual(result["status"], "PROVIDERS_INSUFFICIENT")
        self.assertFalse(result["paper_entry_eligible"])
        self.assertEqual(calls, [])

    def test_two_independent_model_approvals_authorize_only_the_ai_gate(self):
        captured = []
        lock = threading.Lock()

        def fake_post(url, headers, json, timeout, allow_redirects=False):
            with lock:
                captured.append((url, headers, json))
            if "openrouter.ai" in url:
                model = DEFAULT_FREE_MODEL
            elif "googleapis.com" in url:
                model = "gemini-3.8-flash"
            else:
                model = "openai/gpt-oss-20b"
            return MultiProviderAIApproval(model, valid_document())

        with patch.dict(os.environ, {
            "OPENROUTER_API_KEY": "openrouter-secret",
            "GEMINI_API_KEY": "gemini-secret",
        }, clear=True):
            result = run_ai_supervisor(valid_result(), "perp:BTCUSDT", http_post=fake_post)

        self.assertEqual(result["status"], "APPROVED")
        self.assertTrue(result["paper_entry_eligible"])
        self.assertEqual(result["approved_provider_count"], 2)
        self.assertFalse(result["orders_submitted"])
        self.assertFalse(result["trade_executed"])
        self.assertEqual(len(captured), 2)
        body = json.dumps(result)
        self.assertNotIn("openrouter-secret", body)
        self.assertNotIn("gemini-secret", body)
        for url, headers, payload in captured:
            serialized = json.dumps(payload, ensure_ascii=False)
            self.assertNotIn("private_key", serialized)
            self.assertNotIn("wallet_address", serialized)
            self.assertTrue(headers["Authorization"].startswith("Bearer "))

    def test_any_valid_veto_overrides_another_provider_approval(self):
        def fake_post(url, headers, json, timeout, allow_redirects=False):
            model = DEFAULT_FREE_MODEL if "openrouter.ai" in url else "gemini-3.8-flash"
            document = valid_document()
            if "googleapis.com" in url:
                document = valid_document(rating="Hold", proposal="NO_TRADE")
            return MultiProviderAIApproval(model, document)

        with patch.dict(os.environ, {
            "OPENROUTER_API_KEY": "openrouter-secret",
            "GEMINI_API_KEY": "gemini-secret",
        }, clear=True):
            result = run_ai_supervisor(valid_result(), "perp:BTCUSDT", http_post=fake_post)

        self.assertEqual(result["status"], "VETO")
        self.assertFalse(result["paper_entry_eligible"])
        self.assertEqual(result["veto_provider_count"], 1)

    def test_one_approval_plus_one_provider_failure_is_not_enough(self):
        def fake_post(url, headers, json, timeout, allow_redirects=False):
            if "openrouter.ai" in url:
                return MultiProviderAIApproval(DEFAULT_FREE_MODEL, valid_document())
            return MultiProviderAIApproval("gemini-3.8-flash", {}, status_code=429)

        with patch.dict(os.environ, {
            "OPENROUTER_API_KEY": "openrouter-secret",
            "GEMINI_API_KEY": "gemini-secret",
        }, clear=True):
            result = run_ai_supervisor(valid_result(), "perp:BTCUSDT", http_post=fake_post)

        self.assertEqual(result["status"], "UNKNOWN")
        self.assertFalse(result["paper_entry_eligible"])
        self.assertEqual(result["approved_provider_count"], 1)

    def test_native_council_veto_prevents_all_external_provider_calls(self):
        candidate = valid_result()
        candidate.gates[0].status = "UNKNOWN"
        calls = []
        with patch.dict(os.environ, {
            "OPENROUTER_API_KEY": "openrouter-secret",
            "GEMINI_API_KEY": "gemini-secret",
        }, clear=True):
            result = run_ai_supervisor(
                candidate, "perp:BTCUSDT",
                http_post=lambda *args, **kwargs: calls.append(args),
            )
        self.assertIn(result["status"], {"VETO", "UNKNOWN"})
        self.assertFalse(result["paper_entry_eligible"])
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()


class Challenge1V2EasyDemoGroqApprovalTests(unittest.TestCase):
    def test_single_verified_groq_can_approve_with_elliott_as_advisory_context(self):
        from types import SimpleNamespace
        from crypto_super_radar.ai_supervisor import run_challenge1_v2_supervisor_reviews

        review = {
            "provider": "Groq",
            "model": "openai/gpt-oss-20b",
            "returned_model": "openai/gpt-oss-20b",
            "status": "APPROVED",
            "paper_entry_eligible": True,
            "request_attempted": True,
            "response_source": "LIVE_API",
            "response_validated": True,
            "model_verified": True,
            "expected_side": "LONG",
            "trader_proposal": "LONG",
            "elliott_wave_phase": "NO_RULE_VALID_IMPULSE",
            "elliott_wave_direction": "AMBIGUOUS",
        }
        analysis = SimpleNamespace(
            signal="LONG",
            metrics={
                "execution_profile": "CHALLENGE_1_V2",
                "challenge1_v2_evidence": {
                    "elliott_wave": {
                        "phase": "NO_RULE_VALID_IMPULSE",
                        "pattern": "AMBIGUOUS",
                        "wave_alignment": "UNCONFIRMED",
                    }
                },
            },
        )
        with patch.dict(os.environ, {
            "CHALLENGE_1_V2_EASY_DEMO_ENTRY_MODE": "true",
            "BLOFIN_DEMO_TRADING_ENABLED": "true",
        }), patch("crypto_super_radar.ai_supervisor._ai.run_independent_ai_supervisor",
                  return_value=review):
            result = run_challenge1_v2_supervisor_reviews(
                analysis, "perp:BTCUSDT", signal_id="easy-demo-test"
            )
        self.assertTrue(result["approved"])
        self.assertEqual(result["ai_consensus"]["status"], "APPROVED_BY_GROQ")
        self.assertTrue(result["ai_consensus"]["single_reviewer_approval"])
        self.assertTrue(result["ai_consensus"]["elliott_is_advisory"])
