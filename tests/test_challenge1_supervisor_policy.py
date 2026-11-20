from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from crypto_super_radar.challenge1_supervisor_policy import coordinate_position_supervisors


def _review(reviewer_id, action, **extra):
    verdict_by_action = {
        "HOLD_POSITION": "SUPPORTS_DECISION",
        "TIGHTEN_STOP": "CONCERNS",
        "CLOSE_POSITION": "CONCERNS",
        "URGENT_RISK_ALERT": "CONCERNS",
        "REQUEST_DATA": "INSUFFICIENT_DATA",
    }
    return {
        "reviewer_id": reviewer_id,
        "status": "REVIEWED",
        "request_attempted": True,
        "response_validated": True,
        "model_verified": True,
        "evidence_hash": "hash-1",
        "completed_at_utc": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "verdict": verdict_by_action[action],
        "risk_flags": ["test-risk"] if action in {
            "TIGHTEN_STOP", "CLOSE_POSITION", "URGENT_RISK_ALERT"
        } else [],
        **extra,
    }


class Challenge1SupervisorPolicyTests(unittest.TestCase):
    def coordinate(self, a, b, **overrides):
        params = {
            "reviewers": [a, b],
            "evidence_hash": "hash-1",
            "experience_id": "exp-1",
            "position_id": "pos-1",
            "current_position_id": "pos-1",
        }
        params.update(overrides)
        return coordinate_position_supervisors(**params)

    def test_both_hold_is_reported_as_agreed_no_action(self):
        result = self.coordinate(_review("nemotron", "HOLD_POSITION"), _review("second_ai", "HOLD_POSITION"))
        self.assertEqual(result["status"], "AGREED_NO_ACTION")
        self.assertFalse(result["execution_authorized"])

    def test_close_requires_both_independent_reviewers(self):
        result = self.coordinate(_review("nemotron", "CLOSE_POSITION"), _review("second_ai", "CLOSE_POSITION"))
        self.assertEqual(result["status"], "AUTHORIZED")
        self.assertTrue(result["execution_authorized"])
        self.assertEqual(result["action"], "CLOSE_POSITION")

    def test_different_actions_fail_closed(self):
        result = self.coordinate(_review("nemotron", "CLOSE_POSITION"), _review("second_ai", "HOLD_POSITION"))
        self.assertEqual(result["status"], "DISAGREEMENT")
        self.assertFalse(result["execution_authorized"])

    def test_evidence_version_mismatch_blocks_action(self):
        second = _review("second_ai", "CLOSE_POSITION")
        second["evidence_hash"] = "older-hash"
        result = self.coordinate(_review("nemotron", "CLOSE_POSITION"), second)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertFalse(result["execution_authorized"])

    def test_single_reviewer_never_authorizes(self):
        result = self.coordinate(_review("nemotron", "CLOSE_POSITION"), None)
        self.assertFalse(result["execution_authorized"])

    def test_position_id_mismatch_blocks_action(self):
        result = self.coordinate(
            _review("nemotron", "CLOSE_POSITION"),
            _review("second_ai", "CLOSE_POSITION"),
            current_position_id="different-pos",
        )
        self.assertEqual(result["reason_code"], "POSITION_LINK_MISMATCH")

    def test_urgent_alert_blocks_entries_but_does_not_market_close(self):
        result = self.coordinate(
            _review("nemotron", "URGENT_RISK_ALERT"),
            _review("second_ai", "HOLD_POSITION"),
        )
        self.assertEqual(result["status"], "RISK_ALERT")
        self.assertTrue(result["block_new_entries"])
        self.assertFalse(result["execution_authorized"])

    def test_stale_ai_votes_cannot_authorize_position_management(self):
        stale = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
        result = self.coordinate(
            _review("nemotron", "CLOSE_POSITION", completed_at_utc=stale),
            _review("second_ai", "CLOSE_POSITION"),
        )
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["reason_code"], "NEMOTRON_RESULT_STALE")
        self.assertFalse(result["execution_authorized"])

    def test_stop_requires_same_positive_level_from_both(self):
        result = self.coordinate(
            _review("nemotron", "TIGHTEN_STOP", proposed_stop_loss_price="101.2"),
            _review("second_ai", "TIGHTEN_STOP", proposed_stop_loss_price="101.20"),
        )
        self.assertTrue(result["execution_authorized"])
        self.assertEqual(result["proposed_stop_loss_price"], "101.2")

    def test_different_stop_levels_do_not_authorize(self):
        result = self.coordinate(
            _review("nemotron", "TIGHTEN_STOP", proposed_stop_loss_price="101"),
            _review("second_ai", "TIGHTEN_STOP", proposed_stop_loss_price="102"),
        )
        self.assertEqual(result["status"], "DISAGREEMENT")
        self.assertFalse(result["execution_authorized"])


if __name__ == "__main__":
    unittest.main()
