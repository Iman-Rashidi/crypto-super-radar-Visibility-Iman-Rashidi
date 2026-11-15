from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from crypto_super_radar.learning_memory import LearningMemory


class LearningMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "learning.sqlite3")
        self.memory = LearningMemory(self.path)

    def tearDown(self):
        self.memory.close()
        self.tmp.cleanup()

    def test_records_episode_and_survives_reopen(self):
        row = self.memory.record_episode(
            case_id="case-1", engine_id="AGGRESSIVE_HIGH_RISK",
            protocol_stage="BOT_ONLY",
            snapshot={"close": 100.0, "secret_key": "must-not-be-stored"},
            decision={"signal": "LONG", "confidence": 0.7},
            asset="BTCUSDT", observed_at_ms=1_800_000_000_000,
        )
        self.assertTrue(row["episode_id"])
        self.memory.close()
        self.memory = LearningMemory(self.path)
        status = self.memory.status()
        self.assertEqual(status["episodes_total"], 1)
        episode = self.memory.list_episodes(1)[0]
        self.assertNotIn("secret_key", episode["snapshot"])
        self.assertEqual(episode["decision"]["signal"], "LONG")

    def test_ai_mentor_requires_same_frozen_snapshot_and_bot_only_first(self):
        snapshot = {"close": 100.0, "bar": "2026-11-15T10:00:00Z"}
        timestamp = 1_800_000_000_000
        self.memory.record_episode(
            case_id="paired", engine_id="STRICT_12_GATE", protocol_stage="BOT_ONLY",
            snapshot=snapshot, decision={"signal": "NO_TRADE"}, observed_at_ms=timestamp,
        )
        with self.assertRaisesRegex(ValueError, "exact frozen"):
            self.memory.record_episode(
                case_id="paired", engine_id="STRICT_12_GATE",
                protocol_stage="BOT_WITH_AI_MENTOR", snapshot={"close": 101.0},
                decision={"signal": "LONG"}, observed_at_ms=timestamp,
            )
        accepted = self.memory.record_episode(
            case_id="paired", engine_id="STRICT_12_GATE",
            protocol_stage="BOT_WITH_AI_MENTOR", snapshot=snapshot,
            decision={"signal": "NO_TRADE", "mentor_review": "CHALLENGE"},
            observed_at_ms=timestamp,
        )
        self.assertEqual(accepted["snapshot_hash"], self.memory.list_episodes(2)[-1]["snapshot_hash"])

    def test_mentor_cannot_precede_bot_only(self):
        with self.assertRaisesRegex(ValueError, "BOT_ONLY"):
            self.memory.record_episode(
                case_id="late", engine_id="AGGRESSIVE_HIGH_RISK",
                protocol_stage="BOT_WITH_AI_MENTOR", snapshot={"x": 1},
                decision={"y": 2},
            )

    def test_outcome_is_recorded_once(self):
        row = self.memory.record_episode(
            case_id="outcome", engine_id="AGGRESSIVE_HIGH_RISK",
            protocol_stage="PAPER_CYCLE", snapshot={"price": 1},
            decision={"status": "OPENED"},
        )
        self.assertTrue(self.memory.complete_episode(row["episode_id"], {"net_pnl": 3.2}))
        self.assertFalse(self.memory.complete_episode(row["episode_id"], {"net_pnl": 99}))
        self.assertEqual(self.memory.list_episodes(1)[0]["outcome"]["net_pnl"], 3.2)

    def test_lessons_are_not_validated_automatically(self):
        lesson = self.memory.add_lesson(
            source_case_id="case", title="Confirmed setup",
            condition={"regime": "TREND"}, evidence={"samples": 50},
            root_cause="verified evidence", application_scope="BTC 1h",
        )
        self.assertEqual(self.memory.status()["lessons_pending"], 1)
        self.assertTrue(self.memory.validate_lesson(lesson, accepted=True))
        self.assertFalse(self.memory.validate_lesson(lesson, accepted=False))
        self.assertEqual(self.memory.status()["lessons_validated"], 1)


if __name__ == "__main__":
    unittest.main()
