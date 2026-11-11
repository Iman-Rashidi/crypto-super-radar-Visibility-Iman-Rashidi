"""Tests for conservative discovery-stage contract screening."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from crypto_super_radar import gem_hunter_security as security


class FakeSolanaRpc:
    def __init__(self, mint_active=False, freeze_active=False):
        self.mint_active = mint_active
        self.freeze_active = freeze_active

    def token_security(self, address):
        return {
            "mint_authority_active": self.mint_active,
            "freeze_authority_active": self.freeze_active,
            "top10_token_account_concentration_pct": 42.0,
            "scope_note": "Token accounts are not beneficial owners.",
        }


class FakeRugCheckHttp:
    def __init__(self, report):
        self.report = report
        self.calls = []

    def get(self, url, params=None):
        self.calls.append(url)
        return self.report


class GemHunterSecurityTests(unittest.TestCase):
    def setUp(self):
        security._CACHE.clear()

    def test_base_uses_goplus_and_sourcify_result(self):
        data = SimpleNamespace(http=SimpleNamespace(config=SimpleNamespace(request_timeout_seconds=5), limiter="limiter"))
        fake = SimpleNamespace(
            status="PASS", reasons=[], source_verified=True, flags={"is_honeypot": False},
            buy_tax_pct=0.0, sell_tax_pct=0.0, top10_holder_pct=20.0, evidence={},
        )
        with patch.object(security, "screen_evm_token", return_value=fake) as screen:
            result = security.screen_discovery_candidate(
                {"chain": "base", "token_address": "0x" + "1" * 40}, data
            )
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["provider"], "GOPLUS_SOURCIFY")
        screen.assert_called_once()

    def test_base_security_failure_is_preserved(self):
        data = SimpleNamespace(http=SimpleNamespace(config=SimpleNamespace(request_timeout_seconds=5), limiter="limiter"))
        fake = SimpleNamespace(
            status="FAIL", reasons=["GoPlus flags a honeypot."], source_verified=True,
            flags={"is_honeypot": True}, buy_tax_pct=0.0, sell_tax_pct=0.0,
            top10_holder_pct=20.0, evidence={},
        )
        with patch.object(security, "screen_evm_token", return_value=fake):
            result = security.screen_discovery_candidate(
                {"chain": "base", "token_address": "0x" + "2" * 40}, data
            )
        self.assertEqual(result["status"], "FAIL")

    def test_solana_revoked_authorities_still_do_not_claim_full_security_pass(self):
        data = SimpleNamespace(solana_rpc=FakeSolanaRpc())
        result = security.screen_discovery_candidate(
            {"chain": "solana", "token_address": "11111111111111111111111111111111"}, data
        )
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["top10_token_account_concentration_pct"], 42.0)

    def test_rugcheck_critical_risk_and_unprotected_lp_fail_solana_screen(self):
        http = FakeRugCheckHttp({
            "score": 87,
            "risks": [{"name": "Suspicious transfer behavior", "level": "critical"}],
            "lpLocked": False,
            "lpBurned": False,
            "topHolders": [{"pct": 0.22}, {"pct": 0.17}],
        })
        data = SimpleNamespace(solana_rpc=FakeSolanaRpc(), http=http)
        result = security.screen_discovery_candidate(
            {"chain": "solana", "token_address": "So11111111111111111111111111111111111111112"}, data
        )
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["provider"], "SOLANA_RPC+RUGCHECK")
        self.assertFalse(result["lp_protected"])
        self.assertEqual(result["rugcheck"]["risk_score"], 87.0)
        self.assertEqual(len(http.calls), 1)

    def test_rugcheck_without_explicit_danger_does_not_create_false_pass(self):
        http = FakeRugCheckHttp({
            "score": 5,
            "risks": [],
            "lpLocked": True,
            "topHolders": [{"pct": 0.2}, {"pct": 0.1}],
        })
        data = SimpleNamespace(solana_rpc=FakeSolanaRpc(), http=http)
        result = security.screen_discovery_candidate(
            {"chain": "solana", "token_address": "So11111111111111111111111111111111111111111"}, data
        )
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertTrue(result["lp_protected"])
        self.assertIsNone(result["rugcheck"]["honeypot"])

    def test_solana_active_authority_fails_security_gate(self):
        data = SimpleNamespace(solana_rpc=FakeSolanaRpc(mint_active=True))
        result = security.screen_discovery_candidate(
            {"chain": "solana", "token_address": "11111111111111111111111111111111"}, data
        )
        self.assertEqual(result["status"], "FAIL")


if __name__ == "__main__":
    unittest.main()
