"""Single-process lock-coordinated Render scheduler for Challenge 1 V2 Demo."""
from __future__ import annotations

import threading
from typing import Any, Callable, Mapping
import os


def scheduler_is_configured(environ: Mapping[str, str] | None = None) -> bool:
    """Fail closed unless the WEB process is explicitly configured for V2 Demo.

    The legacy BLOFIN_DEMO_AUTOPILOT_ENABLED flag must remain false: V2 owns
    the scheduler and the V2 preflight rejects legacy-worker activation.
    """
    env = os.environ if environ is None else environ
    enabled = lambda key: str(env.get(key, "")).strip().lower() == "true"
    role = str(env.get("BLOFIN_DEMO_AUTOPILOT_ROLE", "WEB") or "WEB").strip().upper()
    return (
        role == "WEB"
        and enabled("CHALLENGE_1_V2_DEMO_AUTOPILOT_ENABLED")
        and enabled("BLOFIN_DEMO_TRADING_ENABLED")
        and not enabled("BLOFIN_DEMO_SMOKE_TEST_ENABLED")
        and not enabled("BLOFIN_DEMO_AUTOPILOT_ENABLED")
    )


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
