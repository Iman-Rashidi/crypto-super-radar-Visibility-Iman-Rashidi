"""Single-process lock-coordinated Render scheduler for Challenge 1 V2 Demo."""
from __future__ import annotations

import threading
from typing import Any, Callable


def run_cycle_guarded(cycle_runner: Callable[[], Any], cycle_lock: Any) -> tuple[bool, Any]:
    """Run only if the shared legacy/V2 process lock is available."""
    if not cycle_lock.acquire(blocking=False):
        return False, None
    try:
        return True, cycle_runner()
    finally:
        cycle_lock.release()


def run_scheduler(*, cycle_runner: Callable[[], Any], cycle_lock: Any,
                  interval_seconds: int = 600,
                  stop_event: threading.Event | None = None) -> None:
    """Wait the configured interval between cycles; never overlap executions."""
    interval = max(60, int(interval_seconds))
    stopper = stop_event or threading.Event()
    while not stopper.wait(interval):
        run_cycle_guarded(cycle_runner, cycle_lock)
