"""Thread-safe process-wide per-host request spacing."""

from __future__ import annotations

import threading
import time
from typing import Dict, Optional
from urllib.parse import urlparse


class SharedRateLimiter:
    """Singleton limiter shared by all public data providers in this process."""

    _instance: Optional["SharedRateLimiter"] = None
    _instance_lock = threading.Lock()

    def __new__(cls, intervals: Optional[Dict[str, float]] = None):
        with cls._instance_lock:
            if cls._instance is None:
                obj = super().__new__(cls)
                obj._schedule_lock = threading.Lock()
                obj._next_allowed: Dict[str, float] = {}
                obj._intervals = dict(intervals or {})
                cls._instance = obj
            elif intervals:
                # Do not lower an already configured interval at runtime.
                for host, interval in intervals.items():
                    cls._instance._intervals[host] = max(
                        cls._instance._intervals.get(host, 0.0), float(interval)
                    )
            return cls._instance

    def configure(self, intervals: Dict[str, float]) -> None:
        with self._schedule_lock:
            for host, interval in intervals.items():
                self._intervals[host] = max(
                    self._intervals.get(host, 0.0), float(interval)
                )

    def wait(self, url: str) -> float:
        host = urlparse(url).netloc.lower()
        interval = self._intervals.get(host, 1.0)
        with self._schedule_lock:
            now = time.monotonic()
            scheduled = max(now, self._next_allowed.get(host, now))
            self._next_allowed[host] = scheduled + interval
        delay = max(0.0, scheduled - time.monotonic())
        if delay:
            time.sleep(delay)
        return delay

    def reset_for_tests(self) -> None:
        with self._schedule_lock:
            self._next_allowed.clear()
