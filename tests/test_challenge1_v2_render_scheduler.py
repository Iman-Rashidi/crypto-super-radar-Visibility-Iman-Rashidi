from __future__ import annotations

import threading
import unittest

from crypto_super_radar.challenge1_v2_render_scheduler import run_cycle_guarded


class Challenge1V2RenderSchedulerTests(unittest.TestCase):
    def test_v2_scheduler_accepts_master_switch_and_smoke_test_off_in_web_role(self):
        from crypto_super_radar.challenge1_v2_render_scheduler import scheduler_is_configured

        base = {
            "BLOFIN_DEMO_AUTOPILOT_ROLE": "WEB",
            "CHALLENGE_1_V2_DEMO_AUTOPILOT_ENABLED": "true",
            "BLOFIN_DEMO_TRADING_ENABLED": "true",
            "BLOFIN_DEMO_SMOKE_TEST_ENABLED": "false",
            "BLOFIN_DEMO_AUTOPILOT_ENABLED": "true",
        }
        self.assertTrue(scheduler_is_configured(base))
        self.assertFalse(scheduler_is_configured({**base, "BLOFIN_DEMO_AUTOPILOT_ROLE": "WORKER"}))
        self.assertFalse(scheduler_is_configured({**base, "BLOFIN_DEMO_SMOKE_TEST_ENABLED": "true"}))
        self.assertFalse(scheduler_is_configured({**base, "CHALLENGE_1_V2_DEMO_AUTOPILOT_ENABLED": "false"}))
        self.assertFalse(scheduler_is_configured({**base, "BLOFIN_DEMO_TRADING_ENABLED": "false"}))

    def test_scheduled_status_snapshot_reports_enabled_web_600s_schedule(self):
        from datetime import datetime, timezone
        from crypto_super_radar.challenge1_v2_render_scheduler import scheduled_status_fields

        now = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
        state = scheduled_status_fields(now)
        self.assertTrue(state["enabled"])
        self.assertEqual(state["status"], "SCHEDULED")
        self.assertEqual(state["runtime_role"], "WEB")
        self.assertEqual(state["interval_seconds"], 600)
        self.assertTrue(state["next_cycle_scheduled"])
        self.assertEqual(state["next_cycle_at_utc"], "2026-11-23T12:10:00+00:00")

    def test_cycle_runs_and_releases_lock(self):
        lock = threading.Lock()
        calls = []
        ran, result = run_cycle_guarded(lambda: calls.append("v2") or "ok", lock)
        self.assertTrue(ran)
        self.assertEqual(result, "ok")
        self.assertEqual(calls, ["v2"])
        self.assertFalse(lock.locked())

    def test_shared_lock_contention_skips_duplicate_cycle(self):
        lock = threading.Lock()
        lock.acquire()
        calls = []
        try:
            ran, result = run_cycle_guarded(lambda: calls.append("duplicate"), lock)
            self.assertFalse(ran)
            self.assertIsNone(result)
            self.assertEqual(calls, [])
        finally:
            lock.release()

    def test_exception_releases_lock(self):
        lock = threading.Lock()
        with self.assertRaises(RuntimeError):
            run_cycle_guarded(lambda: (_ for _ in ()).throw(RuntimeError("test")), lock)
        self.assertFalse(lock.locked())


if __name__ == "__main__":
    unittest.main()
