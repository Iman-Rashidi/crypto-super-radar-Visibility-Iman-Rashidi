"""Atomic integration tests for the paper ledger and learning-experience journal."""
from __future__ import annotations

import unittest

from crypto_super_radar.paper_trading import PaperLedger, _paper_experience_id


def _position(signal_bar: str = "2026-11-16T10:00:00+00:00") -> dict:
    return {
        "asset": "cex:BTCUSDT",
        "quantity": 0.1,
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "initial_stop_loss": 90.0,
        "take_profit": 120.0,
        "entry_fee": 0.08,
        "opened_at": 1_795_000_000,
        "signal_bar": signal_bar,
        "confirmation_score": 82.0,
        "risk_budget": 10.0,
        "max_holding_days": 14,
        "learning_model_version": "test-model-v1",
        "learning_dataset_version": "test-dataset-v1",
        "learning_snapshot": {
            "signal_bar": signal_bar,
            "timeframe": "1h",
            "feature_snapshot_status": "PARTIAL_NOT_FOR_CHALLENGE_SCORING",
            "future_data_in_input": False,
        },
    }


class PaperLearningBridgeTests(unittest.TestCase):
    def setUp(self):
        self.ledger = PaperLedger(path=":memory:", initial_equity_usd=1_000.0)

    def tearDown(self):
        self.ledger.close()

    def test_open_and_final_close_append_one_verified_learning_experience(self):
        position = _position()
        self.assertTrue(self.ledger.open_position(position, debit=10.08))

        experience_id = _paper_experience_id(position["asset"], position["signal_bar"])
        pending = self.ledger.learning_memory.get_experience(experience_id)
        self.assertEqual(pending["learning_status"], "PENDING_OUTCOME")
        self.assertEqual([e["event_type"] for e in pending["events"]], ["DECISION_RECORDED"])
        self.assertEqual(pending["challenge_id"], "PAPER_TRADING_EXPERIENCE")
        self.assertEqual(pending["environment"], "PAPER")

        closed = self.ledger.close_position(110.0, 0.0008, "TEST_EXIT")
        self.assertIsNotNone(closed)
        completed = self.ledger.learning_memory.get_experience(experience_id)
        self.assertEqual(completed["learning_status"], "OUTCOME_RECORDED")
        self.assertEqual(
            [e["event_type"] for e in completed["events"]],
            ["DECISION_RECORDED", "OUTCOME_LABELLED"],
        )
        outcome = completed["events"][-1]["payload"]
        self.assertTrue(outcome["final"])
        self.assertEqual(outcome["decision_correctness"], "UNKNOWN")
        self.assertEqual(outcome["realized_result"], "PROFIT")
        self.assertGreater(outcome["realized_net_pnl"], 0)
        self.assertEqual(completed["verification"]["status"], "PASS")

    def test_decision_and_entry_projection_roll_back_together(self):
        position = _position()
        original = self.ledger.learning_memory.record_decision

        def fail(**kwargs):
            raise RuntimeError("injected learning journal failure")

        self.ledger.learning_memory.record_decision = fail
        try:
            with self.assertRaisesRegex(RuntimeError, "learning journal failure"):
                self.ledger.open_position(position, debit=10.08)
        finally:
            self.ledger.learning_memory.record_decision = original

        self.assertIsNone(self.ledger.position())
        self.assertEqual(self.ledger.execution_ledger.event_count(), 0)
        self.assertEqual(
            self.ledger._db.execute(
                "SELECT COUNT(*) FROM learning_experience_events"
            ).fetchone()[0],
            0,
        )

    def test_legacy_open_position_is_backfilled_without_fabricating_features(self):
        position = _position("legacy-signal-bar")
        original = self.ledger.learning_memory.record_decision
        self.ledger.learning_memory.record_decision = lambda **kwargs: None
        try:
            self.assertTrue(self.ledger.open_position(position, debit=10.08))
        finally:
            self.ledger.learning_memory.record_decision = original

        self.ledger.close_position(95.0, 0.0008, "LEGACY_TEST_EXIT")
        experience = self.ledger.learning_memory.get_experience(
            _paper_experience_id(position["asset"], position["signal_bar"])
        )
        self.assertEqual(experience["learning_status"], "OUTCOME_RECORDED")
        snapshot = experience["events"][0]["payload"]["input_snapshot"]
        self.assertTrue(snapshot["legacy_backfill"])
        self.assertEqual(snapshot["feature_snapshot_status"], "LEGACY_NOT_CAPTURED")
        self.assertEqual(experience["verification"]["status"], "PASS")

    def test_final_outcome_and_close_projection_roll_back_together(self):
        position = _position()
        self.assertTrue(self.ledger.open_position(position, debit=10.08))
        original = self.ledger.learning_memory.record_outcome

        def fail(**kwargs):
            raise RuntimeError("injected final outcome failure")

        self.ledger.learning_memory.record_outcome = fail
        try:
            with self.assertRaisesRegex(RuntimeError, "final outcome failure"):
                self.ledger.close_position(110.0, 0.0008, "TEST_EXIT")
        finally:
            self.ledger.learning_memory.record_outcome = original

        self.assertIsNotNone(self.ledger.position())
        self.assertEqual(
            self.ledger._db.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0],
            0,
        )
        self.assertEqual(self.ledger.learning_memory.get_experience(
            _paper_experience_id(position["asset"], position["signal_bar"])
        )["learning_status"], "PENDING_OUTCOME")


if __name__ == "__main__":
    unittest.main()
