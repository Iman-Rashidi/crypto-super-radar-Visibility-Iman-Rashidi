from __future__ import annotations

import unittest
from types import SimpleNamespace

from crypto_super_radar.agent_council import build_native_agent_council_review


def result_for(profile: str):
    required = {
        "EASY": {1, 2, 3, 4, 5, 6, 9},
        "STANDARD": {1, 2, 3, 4, 5, 6, 8, 9, 10},
        "STRICT": set(range(1, 13)),
    }[profile]
    gates = [
        SimpleNamespace(
            gate_id=gate_id,
            name_en=f"Gate {gate_id}",
            name_fa=f"فیلتر {gate_id}",
            status="PASS" if gate_id in required else "UNKNOWN",
            evidence={},
        )
        for gate_id in range(1, 13)
    ]
    return SimpleNamespace(
        asset="perp:BTCUSDT",
        signal="LONG",
        confirmation_score={"EASY": 70, "STANDARD": 70, "STRICT": 90}[profile],
        estimated_risk_score=20,
        entry_price=100.0,
        stop_loss=98.0,
        take_profit=104.0,
        metrics={"execution_profile": profile},
        gates=gates,
    )


class AgentCouncilProfilePolicyTests(unittest.TestCase):
    def test_easy_allows_non_required_research_gates_to_remain_unknown(self):
        result = build_native_agent_council_review(result_for("EASY"))
        self.assertEqual(result["status"], "APPROVED")
        self.assertTrue(result["paper_entry_eligible"])
        self.assertEqual(result["execution_profile"], "EASY")
        self.assertEqual(result["required_gate_ids"], [1, 2, 3, 4, 5, 6, 9])
        self.assertIn("تحلیل‌گر خبر و احساسات", result["debate"]["abstentions"])

    def test_easy_does_not_veto_an_advisory_research_failure(self):
        candidate = result_for("EASY")
        candidate.gates[6].status = "FAIL"
        review = build_native_agent_council_review(candidate)
        self.assertEqual(review["status"], "APPROVED")
        self.assertTrue(review["paper_entry_eligible"])
        self.assertIn("تحلیل‌گر خبر و احساسات", review["debate"]["bearish_objections"])

    def test_standard_keeps_nine_gate_policy(self):
        result = build_native_agent_council_review(result_for("STANDARD"))
        self.assertEqual(result["status"], "APPROVED")
        self.assertEqual(result["required_gate_ids"], [1, 2, 3, 4, 5, 6, 8, 9])

    def test_strict_vetoes_any_unknown_gate(self):
        candidate = result_for("STRICT")
        candidate.gates[11].status = "UNKNOWN"
        result = build_native_agent_council_review(candidate)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertFalse(result["paper_entry_eligible"])
        self.assertEqual(result["required_gate_ids"], list(range(1, 13)))


if __name__ == "__main__":
    unittest.main()
