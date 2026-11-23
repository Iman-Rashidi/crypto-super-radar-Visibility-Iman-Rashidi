from __future__ import annotations

import threading
import unittest

from crypto_super_radar.challenge1_v2_render_scheduler import run_cycle_guarded


class Challenge1V2RenderSchedulerTests(unittest.TestCase):
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
