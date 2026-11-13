from __future__ import annotations

import unittest
from datetime import datetime, timezone

from crypto_super_radar.token_risk import REQUIRED_TOKEN_RISK_CHECKS, evaluate_token_security


NOW = datetime(2026, 11, 12, 10, 1, tzinfo=timezone.utc)


def complete_evidence():
    return {
        name: {
            "status": "PASS", "source": f"source-{name}",
            "checked_at": "2026-11-12T10:00:00Z", "coverage_pct": 100,
            "value": {"observed": True},
        }
        for name in REQUIRED_TOKEN_RISK_CHECKS
    }


class TokenRiskTests(unittest.TestCase):
    def test_missing_check_is_unknown_not_zero_risk(self):
        result = evaluate_token_security({}, now=NOW)
        self.assertEqual(result["security_gate_status"], "UNKNOWN")
        self.assertEqual(result["incomplete_check_count"], 5)
        self.assertEqual(result["trade_authorization"], "NO_TRADE")

    def test_complete_source_backed_evidence_passes_security_gate_but_never_authorizes_trade(self):
        result = evaluate_token_security(complete_evidence(), now=NOW)
        self.assertEqual(result["security_gate_status"], "PASS")
        self.assertEqual(result["passed_check_count"], 5)
        self.assertEqual(result["trade_authorization"], "NO_TRADE")

    def test_partial_coverage_cannot_be_marked_pass(self):
        evidence = complete_evidence()
        evidence["holder_concentration"]["coverage_pct"] = 74.5
        result = evaluate_token_security(evidence, now=NOW)
        self.assertEqual(result["security_gate_status"], "UNKNOWN")
        self.assertEqual(result["checks"]["holder_concentration"]["status"], "UNKNOWN")

    def test_naive_timestamp_does_not_support_pass(self):
        evidence = complete_evidence()
        evidence["deployer_history"]["checked_at"] = "2026-11-12T10:00:00"
        result = evaluate_token_security(evidence, now=NOW)
        self.assertEqual(result["checks"]["deployer_history"]["status"], "UNKNOWN")

    def test_fail_without_source_metadata_is_downgraded_to_unknown(self):
        evidence = complete_evidence()
        evidence["launch_bundle"] = {"status": "FAIL", "value": {"bundled": True}}
        result = evaluate_token_security(evidence, now=NOW)
        self.assertEqual(result["checks"]["launch_bundle"]["status"], "UNKNOWN")
        self.assertEqual(result["security_gate_status"], "UNKNOWN")

    def test_source_backed_hard_disqualifier_fails_gate(self):
        evidence = complete_evidence()
        evidence["contract_authority"]["status"] = "FAIL"
        evidence["contract_authority"]["value"] = {"mint_authority_active": True}
        result = evaluate_token_security(evidence, now=NOW)
        self.assertEqual(result["security_gate_status"], "FAIL")

    def test_stale_source_evidence_cannot_pass_security_gate(self):
        evidence = complete_evidence()
        evidence["contract_authority"]["checked_at"] = "2026-11-12T09:50:00Z"
        result = evaluate_token_security(evidence, now=NOW)
        self.assertEqual(result["checks"]["contract_authority"]["status"], "STALE")
        self.assertEqual(result["security_gate_status"], "UNKNOWN")
        self.assertEqual(result["incomplete_check_count"], 1)

    def test_far_future_source_timestamp_is_stale(self):
        evidence = complete_evidence()
        evidence["holder_concentration"]["checked_at"] = "2026-11-12T10:30:00Z"
        result = evaluate_token_security(evidence, now=NOW)
        self.assertEqual(result["checks"]["holder_concentration"]["status"], "STALE")
        self.assertEqual(result["security_gate_status"], "UNKNOWN")

    def test_budget_deferred_check_remains_unknown(self):
        evidence = complete_evidence()
        evidence["holder_concentration"] = {
            "status": "NOT_CHECKED_BUDGET", "source": "goplus",
            "checked_at": "2026-11-12T10:00:00Z", "coverage_pct": 0,
            "value": None,
        }
        result = evaluate_token_security(evidence, now=NOW)
        self.assertEqual(result["checks"]["holder_concentration"]["status"], "NOT_CHECKED_BUDGET")
        self.assertEqual(result["security_gate_status"], "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
