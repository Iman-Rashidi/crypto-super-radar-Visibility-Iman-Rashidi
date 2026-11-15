from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest

from crypto_super_radar.learning_experience_memory import (
    ExperienceIdempotencyConflict,
    ExperienceIntegrityError,
    ExperienceMemoryError,
    ExperienceStateError,
    LearningExperienceMemory,
)


class LearningExperienceMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tempdir.name, "learning-memory.sqlite3")
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.memory = LearningExperienceMemory(self.db)

    def tearDown(self):
        self.db.close()
        self.tempdir.cleanup()

    def decision(self, experience_id="exp-001", event_id="decision-001", **overrides):
        values = {
            "experience_id": experience_id,
            "event_id": event_id,
            "engine_id": "EASY",
            "challenge_id": "CHALLENGE-01",
            "cycle_id": "cycle-001",
            "decision_mode": "BOT_ONLY",
            "environment": "HISTORICAL_REPLAY",
            "market": "SPOT",
            "symbol": "BTCUSDT",
            "timeframe": "1H",
            "model_version": "baseline-0",
            "dataset_version": "dataset-v1",
            "decision": {"action": "NO_SIGNAL", "confidence": 0.72},
            "input_snapshot": {"close": 65000.0, "candle_closed": True},
            "timestamp_utc": "2026-11-15T10:00:00Z",
        }
        values.update(overrides)
        return self.memory.record_decision(**values)

    def outcome(
        self,
        experience_id="exp-001",
        event_id="outcome-001",
        correctness="CORRECT",
        result="PROFIT",
        pnl=5.0,
        timestamp="2026-11-15T10:01:00Z",
    ):
        return self.memory.record_outcome(
            experience_id=experience_id,
            event_id=event_id,
            decision_correctness=correctness,
            realized_result=result,
            realized_net_pnl=pnl,
            evidence={"source": "closed_paper_trade", "trade_id": "paper-123"},
            timestamp_utc=timestamp,
        )

    def test_experience_survives_database_close_and_reopen(self):
        with self.db:
            self.decision()
            self.outcome()
        expected = self.memory.get_experience("exp-001")
        expected_hashes = [item["event_hash"] for item in expected["events"]]
        self.db.close()

        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.memory = LearningExperienceMemory(self.db)
        recovered = self.memory.get_experience("exp-001")

        self.assertEqual(recovered["learning_status"], "OUTCOME_RECORDED")
        self.assertEqual(
            [item["event_hash"] for item in recovered["events"]],
            expected_hashes,
        )
        self.assertEqual(recovered["verification"]["status"], "PASS")

    def test_identical_event_retry_is_idempotent(self):
        with self.db:
            first = self.decision()
            repeated = self.decision()
        self.assertTrue(first["inserted"])
        self.assertFalse(repeated["inserted"])
        self.assertEqual(first["event_hash"], repeated["event_hash"])
        self.assertEqual(len(self.memory.get_experience("exp-001")["events"]), 1)

    def test_event_id_reuse_with_different_content_is_rejected(self):
        with self.db:
            self.decision()
            with self.assertRaises(ExperienceIdempotencyConflict):
                self.decision(
                    decision={"action": "BUY", "confidence": 0.99},
                )

    def test_only_one_decision_per_engine_challenge_cycle_and_mode(self):
        with self.db:
            self.decision()
            with self.assertRaises(sqlite3.IntegrityError):
                self.decision(
                    experience_id="duplicate-cycle",
                    event_id="duplicate-cycle-decision",
                )

    def test_four_decision_outcome_combinations_are_distinguished(self):
        examples = [
            ("correct-profit", "CORRECT", "PROFIT", 5.0, "CORRECT_PROFIT"),
            ("correct-loss", "CORRECT", "LOSS", -3.0, "CORRECT_LOSS"),
            ("wrong-lucky", "INCORRECT", "PROFIT", 2.0, "INCORRECT_PROFIT_LUCK"),
            ("wrong-loss", "INCORRECT", "LOSS", -4.0, "INCORRECT_LOSS"),
        ]
        with self.db:
            for experience_id, correctness, result, pnl, expected_case in examples:
                self.decision(
                    experience_id=experience_id,
                    event_id=f"{experience_id}-decision",
                    cycle_id=f"cycle-{experience_id}",
                )
                item = self.outcome(
                    experience_id=experience_id,
                    event_id=f"{experience_id}-outcome",
                    correctness=correctness,
                    result=result,
                    pnl=pnl,
                )
                self.assertEqual(item["payload"]["experience_case"], expected_case)

    def test_open_or_nonfinal_pnl_cannot_become_training_label(self):
        with self.db:
            self.decision()
            with self.assertRaises(ExperienceStateError):
                self.memory.record_outcome(
                    experience_id="exp-001",
                    event_id="unrealized-label",
                    decision_correctness="UNKNOWN",
                    realized_result="PROFIT",
                    realized_net_pnl=0.5,
                    final=False,
                    evidence={"source": "open_position_snapshot"},
                )
            with self.assertRaises(ExperienceMemoryError):
                self.memory.record_outcome(
                    experience_id="exp-001",
                    event_id="bad-profit-sign",
                    decision_correctness="CORRECT",
                    realized_result="PROFIT",
                    realized_net_pnl=-1.0,
                    evidence={"source": "closed_trade"},
                )

    def test_outcome_is_final_and_can_only_be_recorded_once(self):
        with self.db:
            self.decision()
            self.outcome()
            with self.assertRaises(ExperienceStateError):
                self.outcome(
                    event_id="second-outcome",
                    correctness="INCORRECT",
                    result="LOSS",
                    pnl=-1.0,
                    timestamp="2026-11-15T10:02:00Z",
                )

    def test_not_executed_is_a_separate_non_trade_outcome(self):
        with self.db:
            self.decision()
            result = self.memory.record_outcome(
                experience_id="exp-001",
                event_id="not-executed",
                decision_correctness="UNKNOWN",
                realized_result="NOT_EXECUTED",
                realized_net_pnl=None,
                evidence={"source": "no_order_submitted"},
            )
        self.assertEqual(result["payload"]["experience_case"], "NOT_EXECUTED")
        self.assertIsNone(result["payload"]["realized_net_pnl"])

    def test_mentor_cannot_review_before_bot_only_outcome_is_locked(self):
        with self.db:
            self.decision()
            with self.assertRaises(ExperienceStateError):
                self.memory.record_mentor_feedback(
                    experience_id="exp-001",
                    event_id="mentor-too-early",
                    mentor_model_version="mentor-v1",
                    feedback="Wait for a completed candle.",
                )

    def test_mentor_baseline_and_second_decision_are_linked(self):
        with self.db:
            self.decision()
            self.outcome()
            self.memory.record_mentor_feedback(
                experience_id="exp-001",
                event_id="mentor-001",
                mentor_model_version="mentor-v1",
                feedback="The candle was incomplete at decision time.",
                proposed_change="Only evaluate completed candles.",
            )
            assisted = self.memory.record_decision(
                experience_id="exp-002",
                event_id="decision-002",
                engine_id="EASY",
                challenge_id="CHALLENGE-01",
                cycle_id="cycle-001",
                decision_mode="BOT_WITH_AI_MENTOR",
                environment="HISTORICAL_REPLAY",
                decision={"action": "WAIT", "confidence": 0.81},
                input_snapshot={"close": 65000.0, "candle_closed": False},
                baseline_experience_id="exp-001",
                model_version="candidate-1",
                dataset_version="dataset-v1",
                timestamp_utc="2026-11-15T10:02:00Z",
            )
        self.assertEqual(assisted["payload"]["baseline_experience_id"], "exp-001")

    def test_mentor_decision_requires_same_challenge_cycle_and_feedback(self):
        with self.db:
            self.decision()
            self.outcome()
            with self.assertRaises(ExperienceStateError):
                self.memory.record_decision(
                    experience_id="assisted-early",
                    event_id="assisted-early-event",
                    engine_id="EASY",
                    challenge_id="CHALLENGE-01",
                    cycle_id="cycle-001",
                    decision_mode="BOT_WITH_AI_MENTOR",
                    environment="HISTORICAL_REPLAY",
                    decision={"action": "WAIT"},
                    input_snapshot={"close": 65000.0},
                    baseline_experience_id="exp-001",
                )
            self.memory.record_mentor_feedback(
                experience_id="exp-001",
                event_id="mentor-001",
                mentor_model_version="mentor-v1",
                feedback="Review completed candle state.",
            )
            with self.assertRaises(ExperienceStateError):
                self.memory.record_decision(
                    experience_id="assisted-wrong-cycle",
                    event_id="assisted-wrong-cycle-event",
                    engine_id="EASY",
                    challenge_id="CHALLENGE-01",
                    cycle_id="cycle-002",
                    decision_mode="BOT_WITH_AI_MENTOR",
                    environment="HISTORICAL_REPLAY",
                    decision={"action": "WAIT"},
                    input_snapshot={"close": 65000.0},
                    baseline_experience_id="exp-001",
                )

    def test_lesson_requires_outcome_and_independent_validation(self):
        with self.db:
            self.decision()
            with self.assertRaises(ExperienceStateError):
                self.memory.propose_lesson(
                    experience_id="exp-001",
                    event_id="lesson-too-early",
                    lesson_id="lesson-1",
                    lesson="Ignore incomplete candles.",
                    evidence_event_ids=["decision-001"],
                )
            self.outcome()
            proposed = self.memory.propose_lesson(
                experience_id="exp-001",
                event_id="lesson-proposed",
                lesson_id="lesson-1",
                lesson="Ignore incomplete candles.",
                evidence_event_ids=["decision-001", "outcome-001"],
            )
            self.assertEqual(proposed["event_type"], "LESSON_PROPOSED")
            with self.assertRaises(ExperienceStateError):
                self.memory.validate_lesson(
                    experience_id="exp-001",
                    event_id="lesson-invalid-validation",
                    lesson_id="lesson-1",
                    independent_test=False,
                    test_dataset_id="holdout-a",
                    sample_count=100,
                    passed=True,
                    metrics={"accuracy": 0.95},
                )
            self.memory.validate_lesson(
                experience_id="exp-001",
                event_id="lesson-validation",
                lesson_id="lesson-1",
                independent_test=True,
                test_dataset_id="holdout-a",
                sample_count=100,
                passed=True,
                metrics={"accuracy": 0.95},
            )
        self.assertEqual(
            self.memory.get_experience("exp-001")["learning_status"],
            "LESSON_VALIDATED",
        )

    def test_same_lesson_cannot_reuse_same_independent_dataset(self):
        with self.db:
            self.decision()
            self.outcome()
            self.memory.propose_lesson(
                experience_id="exp-001",
                event_id="lesson-proposed",
                lesson_id="lesson-1",
                lesson="Test a simple rule.",
                evidence_event_ids=["decision-001", "outcome-001"],
            )
            self.memory.validate_lesson(
                experience_id="exp-001",
                event_id="validation-1",
                lesson_id="lesson-1",
                independent_test=True,
                test_dataset_id="holdout-a",
                sample_count=50,
                passed=False,
                metrics={"accuracy": 0.5},
            )
            with self.assertRaises(ExperienceStateError):
                self.memory.validate_lesson(
                    experience_id="exp-001",
                    event_id="validation-2",
                    lesson_id="lesson-1",
                    independent_test=True,
                    test_dataset_id="holdout-a",
                    sample_count=50,
                    passed=True,
                    metrics={"accuracy": 0.95},
                )

    def test_sensitive_fields_are_rejected_before_persistence(self):
        with self.db:
            with self.assertRaises(ExperienceMemoryError):
                self.decision(
                    input_snapshot={
                        "close": 65000.0,
                        "nested": {"api_secret": "must-not-be-stored"},
                    },
                )
        self.assertEqual(
            self.db.execute(
                "SELECT COUNT(*) FROM learning_experience_events"
            ).fetchone()[0],
            0,
        )

    def test_timestamps_must_be_timezone_aware_and_monotonic(self):
        with self.db:
            with self.assertRaises(ExperienceMemoryError):
                self.decision(timestamp_utc="2026-11-15T10:00:00")
            self.decision()
            with self.assertRaises(ExperienceStateError):
                self.outcome(timestamp="2026-11-15T09:59:59Z")

    def test_event_table_rejects_update_and_delete(self):
        with self.db:
            self.decision()
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                "UPDATE learning_experience_events SET event_type='TAMPERED' "
                "WHERE event_id='decision-001'"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                "DELETE FROM learning_experience_events WHERE event_id='decision-001'"
            )

    def test_hash_chain_detects_payload_tampering(self):
        with self.db:
            self.decision()
            self.outcome()
        self.db.execute("DROP TRIGGER learning_experience_no_update")
        self.db.execute(
            "UPDATE learning_experience_events SET payload_json='{}' "
            "WHERE event_id='outcome-001'"
        )
        self.db.commit()
        result = self.memory.verify_experience("exp-001")
        self.assertEqual(result["status"], "FAIL")
        self.assertTrue(
            any("payload hash mismatch" in item for item in result["errors"])
        )
        with self.assertRaises(ExperienceIntegrityError):
            self.memory.get_experience("exp-001")

    def test_caller_transaction_rolls_back_memory_write(self):
        with self.assertRaises(RuntimeError):
            with self.db:
                self.decision()
                raise RuntimeError("forced rollback")
        self.assertEqual(
            self.db.execute(
                "SELECT COUNT(*) FROM learning_experience_events"
            ).fetchone()[0],
            0,
        )

    def test_two_connections_replay_same_decision_once(self):
        with self.db:
            first = self.decision()
        second_db = sqlite3.connect(self.path)
        second_db.row_factory = sqlite3.Row
        second_memory = LearningExperienceMemory(second_db)
        try:
            with second_db:
                replay = second_memory.record_decision(
                    experience_id="exp-001",
                    event_id="decision-001",
                    engine_id="EASY",
                    challenge_id="CHALLENGE-01",
                    cycle_id="cycle-001",
                    decision_mode="BOT_ONLY",
                    environment="HISTORICAL_REPLAY",
                    market="SPOT",
                    symbol="BTCUSDT",
                    timeframe="1H",
                    model_version="baseline-0",
                    dataset_version="dataset-v1",
                    decision={"action": "NO_SIGNAL", "confidence": 0.72},
                    input_snapshot={"close": 65000.0, "candle_closed": True},
                    timestamp_utc="2026-11-15T10:00:00Z",
                )
            self.assertTrue(first["inserted"])
            self.assertFalse(replay["inserted"])
            self.assertEqual(
                second_memory.verify_experience("exp-001")["event_count"], 1
            )
        finally:
            second_db.close()


if __name__ == "__main__":
    unittest.main()
