from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from crypto_super_radar.challenge1_audit import Challenge1AuditRecorder
from crypto_super_radar import challenge1_position_observers as observers


class _FakeThread:
    def __init__(self, *, target, kwargs, name, daemon):
        self.target = target
        self.kwargs = kwargs
        self.name = name
        self.daemon = daemon
        self.started = False

    def start(self):
        self.started = True

    def is_alive(self):
        # Deliberately do not execute a provider call in unit tests.
        return False


class Challenge1PositionObserverAuditTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        recorder = Challenge1AuditRecorder(connection=self.connection)
        recorder.close()
        observers._REVIEW_THREADS.clear()

    def tearDown(self):
        observers._REVIEW_THREADS.clear()
        self.connection.close()

    def _events(self, reference):
        recorder = Challenge1AuditRecorder(connection=self.connection)
        try:
            return recorder.list_related(reference, limit=2000)
        finally:
            recorder.close()

    def test_each_15_second_monitor_cycle_gets_a_distinct_hash_chained_event(self):
        start = (1_795_000_000_000 // 60_000) * 60_000 + 5_000
        details = {
            "decision": "HOLD",
            "heartbeat_status": "ALIVE",
            "reviewer_states": {
                "nemotron": {"status": "NOT_CALLED", "reason_code": "REVIEW_NOT_DUE"},
                "second_ai": {"status": "NOT_CALLED", "reason_code": "REVIEW_NOT_DUE"},
            },
        }
        with patch.object(observers, "_queue_notion_projection", return_value="QUEUED") as queue_projection:
            first = observers.record_position_monitor_cycle(
                experience_id="experience-1",
                position_id="position-1",
                order_id="order-1",
                symbol="BTC-USDT",
                signal="LONG",
                cycle_details=details,
                cycle_epoch_ms=start,
                connection=self.connection,
            )
            second = observers.record_position_monitor_cycle(
                experience_id="experience-1",
                position_id="position-1",
                order_id="order-1",
                symbol="BTC-USDT",
                signal="LONG",
                cycle_details=details,
                cycle_epoch_ms=start + 15_000,
                connection=self.connection,
            )
            third = observers.record_position_monitor_cycle(
                experience_id="experience-1",
                position_id="position-1",
                order_id="order-1",
                symbol="BTC-USDT",
                signal="LONG",
                cycle_details=details,
                cycle_epoch_ms=start + 60_000,
                connection=self.connection,
            )

        self.assertEqual(first["status"], "RECORDED")
        self.assertEqual(second["status"], "RECORDED")
        self.assertEqual(third["status"], "RECORDED")
        self.assertNotEqual(first["event_id"], second["event_id"])
        report = self._events("experience-1")
        phases = [event["phase"] for event in report["events"]]
        self.assertEqual(phases.count("POSITION_EXIT_EVALUATION"), 3)
        self.assertEqual(phases.count("POSITION_MANAGER_CYCLE_SUMMARY"), 1)
        self.assertEqual(report["integrity"]["status"], "PASS")
        self.assertEqual(report["integrity"]["checked_events"], 4)
        queue_projection.assert_called_once()

    def test_unlinked_position_is_audited_without_fabricating_experience_id(self):
        result = observers.record_position_monitor_cycle(
            experience_id=None,
            position_id="position-unlinked",
            order_id="order-unlinked",
            symbol="SOL-USDT",
            signal="UNKNOWN",
            cycle_details={"decision": "HOLD", "experience_link_status": "MISSING"},
            cycle_epoch_ms=1_795_000_000_000,
            connection=self.connection,
        )
        self.assertEqual(result["status"], "RECORDED")
        self.assertEqual(result["experience_link_status"], "MISSING")
        report = self._events("unlinked-position:position-unlinked")
        self.assertEqual(report["event_count"], 1)
        self.assertIsNone(report["events"][0]["experience_id"])
        self.assertEqual(report["integrity"]["status"], "PASS")

    def test_each_reviewer_not_called_reason_is_a_distinct_event(self):
        first = observers.record_position_observer_skip(
            experience_id=None,
            position_id="position-2",
            reviewer_id="nemotron",
            cycle_id="position-2:1000",
            reason_code="ENTRY_SNAPSHOT_MISSING",
            reason="No persisted entry evidence is available.",
            connection=self.connection,
        )
        second = observers.record_position_observer_skip(
            experience_id=None,
            position_id="position-2",
            reviewer_id="second_ai",
            cycle_id="position-2:1000",
            reason_code="ENTRY_SNAPSHOT_MISSING",
            reason="No persisted entry evidence is available.",
            connection=self.connection,
        )
        self.assertNotEqual(first["event_id"], second["event_id"])
        report = self._events("unlinked-position:position-2")
        skip_events = [
            event for event in report["events"]
            if event["phase"] == "POSITION_AI_REVIEW_SKIPPED"
        ]
        self.assertEqual(len(skip_events), 2)
        reviewers = {
            event["payload"]["details"]["reviewer_id"] for event in skip_events
        }
        self.assertEqual(reviewers, {"nemotron", "second_ai"})
        self.assertTrue(all(
            event["payload"]["details"]["status"] == "NOT_CALLED"
            and event["payload"]["details"]["request_attempted"] is False
            for event in skip_events
        ))
        self.assertEqual(report["integrity"]["status"], "PASS")

    def test_scheduler_requires_position_and_experience_before_any_provider_call(self):
        states = observers.schedule_position_observer_review(
            challenge_record={},
            cycle_context={},
            experience_id="",
            position_id="",
            order_id="",
            symbol="BTC-USDT",
            signal="LONG",
            now=datetime(2026, 11, 19, 0, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(set(states), {"nemotron", "second_ai"})
        self.assertTrue(all(state["status"] == "NOT_CALLED" for state in states.values()))
        self.assertTrue(all(
            state["reason_code"] == "EXPERIENCE_OR_POSITION_ID_MISSING"
            and state["request_attempted"] is False
            for state in states.values()
        ))

    def test_completed_review_batch_survives_restart_and_obeys_persisted_cadence(self):
        first_time = datetime(2026, 11, 19, 0, 0, tzinfo=timezone.utc)
        with (
            patch.object(
                observers, "Challenge1AuditRecorder",
                side_effect=lambda: Challenge1AuditRecorder(connection=self.connection),
            ),
            patch.object(observers.threading, "Thread", _FakeThread),
        ):
            requested = observers.schedule_position_observer_review(
                challenge_record={"signal": "LONG", "challenge1_evidence": {"candles_5m": []}},
                cycle_context={"robot_decision": "HOLD"},
                experience_id="experience-3",
                position_id="position-3",
                order_id="order-3",
                symbol="BTC-USDT",
                signal="LONG",
                now=first_time,
            )
        self.assertTrue(all(state["status"] == "REQUESTED" for state in requested.values()))
        batch_id = requested["nemotron"]["review_batch_id"]

        recorder = Challenge1AuditRecorder(connection=self.connection)
        try:
            for reviewer_id in ("nemotron", "second_ai"):
                recorder.append_event(
                    cycle_id="experience-3",
                    phase="POSITION_AI_REVIEW_RESULT",
                    event_key=f"result:{batch_id}:{reviewer_id}",
                    details={
                        "review_batch_id": batch_id,
                        "reviewer_id": reviewer_id,
                        "status": "REVIEWED",
                        "request_attempted": True,
                        "response_validated": True,
                        "model_verified": True,
                        "response_completed_at_utc": (first_time + timedelta(seconds=4)).isoformat(),
                        "execution_effect": "NONE",
                    },
                    experience_id="experience-3",
                    position_id="position-3",
                )
        finally:
            recorder.close()

        # Simulate a process restart: no in-memory worker state remains.
        observers._REVIEW_THREADS.clear()
        with (
            patch.object(
                observers, "Challenge1AuditRecorder",
                side_effect=lambda: Challenge1AuditRecorder(connection=self.connection),
            ),
            patch.object(observers.threading, "Thread", _FakeThread) as thread_class,
        ):
            later = observers.schedule_position_observer_review(
                challenge_record={"signal": "LONG", "challenge1_evidence": {"candles_5m": []}},
                cycle_context={"robot_decision": "HOLD"},
                experience_id="experience-3",
                position_id="position-3",
                order_id="order-3",
                symbol="BTC-USDT",
                signal="LONG",
                now=first_time + timedelta(minutes=1),
            )
        self.assertTrue(all(state["status"] == "NOT_CALLED" for state in later.values()))
        self.assertTrue(all(state["reason_code"] == "REVIEW_NOT_DUE" for state in later.values()))
        thread_class.assert_not_called()
        report = self._events("experience-3")
        self.assertEqual(report["integrity"]["status"], "PASS")
        self.assertEqual(
            sum(event["phase"] == "POSITION_AI_REVIEW_BATCH_REQUESTED" for event in report["events"]),
            1,
        )


if __name__ == "__main__":
    unittest.main()
