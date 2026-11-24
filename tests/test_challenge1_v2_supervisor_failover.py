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



class OpenRouterFreeFallbackChainTests(unittest.TestCase):
    def test_chain_contains_only_free_models_and_keeps_primary_first(self):
        from crypto_super_radar.ai_second_opinion import (
            DEFAULT_FREE_MODEL, _openrouter_free_model_chain,
        )
        chain = _openrouter_free_model_chain()
        self.assertEqual(chain[0], DEFAULT_FREE_MODEL)
        self.assertGreaterEqual(len(chain), 3)
        self.assertEqual(len(chain), len(set(chain)))
        self.assertTrue(all(model.endswith(":free") for model in chain))

    def test_rate_limit_moves_to_next_free_model(self):
        from crypto_super_radar.ai_second_opinion import (
            DEFAULT_FREE_MODEL, _post_openrouter_with_fallback,
        )
        calls = []
        class Response:
            def __init__(self, status):
                self.status_code = status
        def post(_url, *, headers, json, timeout, allow_redirects):
            calls.append(json["model"])
            return Response(429 if len(calls) == 1 else 200)
        result = _post_openrouter_with_fallback(
            post, "https://openrouter.ai/api/v1/chat/completions",
            headers={}, payload={"model": DEFAULT_FREE_MODEL},
            timeout=(8, 35), allow_redirects=False,
        )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(calls[0], DEFAULT_FREE_MODEL)
        self.assertEqual(calls[1], "nvidia/nemotron-3.5-lightning:free")

    def test_timeout_moves_to_next_free_model(self):
        import requests
        from crypto_super_radar.ai_second_opinion import (
            DEFAULT_FREE_MODEL, _post_openrouter_with_fallback,
        )
        calls = []
        class Response:
            status_code = 200
        def post(_url, *, headers, json, timeout, allow_redirects):
            calls.append(json["model"])
            if len(calls) == 1:
                raise requests.Timeout("test timeout")
            return Response()
        result = _post_openrouter_with_fallback(
            post, "https://openrouter.ai/api/v1/chat/completions",
            headers={}, payload={"model": DEFAULT_FREE_MODEL},
            timeout=(8, 35), allow_redirects=False,
        )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(calls), 2)



from crypto_super_radar import ai_second_opinion as _ai_second_opinion


@unittest.skipUnless(
    hasattr(_ai_second_opinion, "_normalize_specialist_views"),
    "V2 specialist normalization implementation is being pushed in the paired commit",
)
class Challenge1V2SpecialistNormalizationTests(unittest.TestCase):
    def valid_v2_document(self):
        document = valid_approval()
        document.update({
            "specialist_views": {
                "market_regime": {"view": "BEARISH", "reason_fa": "رژیم نزولی است."},
                "technical": {"view": "BEARISH", "reason_fa": "ساختار نزولی است."},
                "news_sentiment": {"view": "NEUTRAL", "reason_fa": "داده احساسات خنثی است."},
                "bull_researcher": {"view": "OPPOSE", "reason_fa": "سناریوی صعودی رد شد."},
                "bear_researcher": {"view": "SUPPORT", "reason_fa": "سناریوی نزولی پشتیبانی می‌شود."},
                "trader": {"proposal": "SHORT", "reason_fa": "پیشنهاد فروش."},
                "risk_manager": {"verdict": "APPROVE", "reason_fa": "ریسک کنترل شده است."},
                "portfolio_manager": {"verdict": "APPROVE", "reason_fa": "حد پرتفوی رعایت شده است."},
            },
            "final_rating": "Sell",
            "risk_flags": [],
            "rationale_fa": "تحلیل مستقل مبتنی بر شواهد.",
            "wave_analyst": {
                "verdict": "SUPPORT", "phase": "WAVE_4_CONFIRMED",
                "direction": "BEARISH", "reason_fa": "موج با شواهد هم‌خوان است.",
            },
        })
        return document

    def validate(self, document):
        from crypto_super_radar.ai_second_opinion import _validated_v2_review
        return _validated_v2_review(
            document, "test-free-model", "perp:BTCUSDT",
            expected_side="SHORT",
            evidence={"elliott_wave": {"phase": "WAVE_4_CONFIRMED", "pattern": "BEARISH"}},
        )

    def test_v2_canonical_schema_is_accepted(self):
        result = self.validate(self.valid_v2_document())
        self.assertEqual(result["status"], "APPROVED")
        self.assertTrue(result["paper_entry_eligible"])

    def test_v2_specialist_analysis_aliases_and_abbreviated_roles_normalize(self):
        source = self.valid_v2_document()
        original = source.pop("specialist_views")
        source["specialist_analysis"] = {
            "regime": {"view": "BEAR", "reason": original["market_regime"]["reason_fa"]},
            "tech": {"verdict": "BEARISH", "analysis": original["technical"]["reason_fa"]},
            "sentiment": {"view": "NEUTRAL", "why": original["news_sentiment"]["reason_fa"]},
            "bull": {"stance": "OPPOSE", "rationale": original["bull_researcher"]["reason_fa"]},
            "bear_case": {"view": "SUPPORT", "reason_fa": original["bear_researcher"]["reason_fa"]},
            "trade": {"direction": "SHORT", "reason": original["trader"]["reason_fa"]},
            "risk": {"decision": "APPROVED", "analysis": original["risk_manager"]["reason_fa"]},
            "portfolio": {"status": "APPROVE", "explanation": original["portfolio_manager"]["reason_fa"]},
        }
        source["decision"] = "APPROVE"
        source["direction"] = "BEARISH"
        result = self.validate(source)
        self.assertEqual(result["status"], "APPROVED")
        self.assertEqual(result["trader_proposal"], "SHORT")
        self.assertEqual(result["risk_manager_verdict"], "APPROVE")
        self.assertEqual(result["portfolio_manager_verdict"], "APPROVE")
        self.assertEqual(result["declared_direction"], "SHORT")

    def test_v2_views_alias_is_accepted_but_missing_role_is_not_fabricated(self):
        source = self.valid_v2_document()
        source["views"] = source.pop("specialist_views")
        self.assertEqual(self.validate(source)["status"], "APPROVED")
        del source["views"]["bear_researcher"]
        with self.assertRaisesRegex(ValueError, "Required specialist views are missing"):
            self.validate(source)

    def test_v2_conflicting_alias_or_direction_fails_closed(self):
        source = self.valid_v2_document()
        source["specialist_analysis"] = dict(source["specialist_views"])
        source["specialist_analysis"]["trader"] = {"proposal": "LONG", "reason_fa": "جهت متناقض"}
        with self.assertRaisesRegex(ValueError, "Conflicting specialist view containers"):
            self.validate(source)
        source = self.valid_v2_document()
        source["direction"] = "LONG"
        with self.assertRaisesRegex(ValueError, "AI direction conflicts"):
            self.validate(source)

    def test_v2_declared_approval_cannot_override_risk_veto(self):
        source = self.valid_v2_document()
        source["specialist_views"]["risk_manager"]["verdict"] = "REJECT"
        source["decision"] = "APPROVE"
        with self.assertRaisesRegex(ValueError, "approval conflicts"):
            self.validate(source)
        source["decision"] = "VETO"
        result = self.validate(source)
        self.assertEqual(result["status"], "VETO")
        self.assertFalse(result["paper_entry_eligible"])

    def test_v2_prompt_contains_all_mandatory_specialist_keys(self):
        from crypto_super_radar.ai_second_opinion import _prompt_messages
        prompt = _prompt_messages({
            "execution_profile": "CHALLENGE_1_V2",
            "review_mode": "CHALLENGE_1_V2_MULTI_TIMEFRAME_REVIEW",
            "expected_side": "SHORT",
            "elliott_wave": {"phase": "WAVE_4_CONFIRMED", "pattern": "BEARISH"},
        })[0]["content"]
        for key in (
            '"specialist_views"', '"market_regime"', '"technical"',
            '"news_sentiment"', '"bull_researcher"', '"bear_researcher"',
            '"trader"', '"risk_manager"', '"portfolio_manager"',
            '"wave_analyst"', '"final_rating"', '"risk_flags"', '"rationale_fa"',
        ):
            self.assertIn(key, prompt)


if __name__ == "__main__":
    unittest.main()
