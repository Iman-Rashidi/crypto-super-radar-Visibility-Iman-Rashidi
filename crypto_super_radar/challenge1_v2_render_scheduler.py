"""Single-process lock-coordinated Render scheduler for Challenge 1 V2 Demo."""
from __future__ import annotations

import threading
from typing import Any, Callable, Mapping
import os


def scheduler_is_configured(environ: Mapping[str, str] | None = None) -> bool:
    """Fail closed unless the WEB process is explicitly enabled for V2 Demo.

    BLOFIN_DEMO_AUTOPILOT_ENABLED is the operator's master switch. V2 owns
    execution when its profile flag is enabled, so it must not be rejected
    merely because the legacy worker is bypassed.
    """
    env = os.environ if environ is None else environ
    enabled = lambda key: str(env.get(key, "")).strip().lower() == "true"
    role = str(env.get("BLOFIN_DEMO_AUTOPILOT_ROLE", "WEB") or "WEB").strip().upper()
    return (
        role == "WEB"
        and enabled("CHALLENGE_1_V2_DEMO_AUTOPILOT_ENABLED")
        and enabled("BLOFIN_DEMO_TRADING_ENABLED")
        and not enabled("BLOFIN_DEMO_SMOKE_TEST_ENABLED")
    )


def scheduled_status_fields(now=None) -> dict[str, Any]:
    """Return a truthful initial schedule snapshot; no cycle is claimed as run."""
    from datetime import datetime, timedelta, timezone

    current = now or datetime.now(timezone.utc)
    next_cycle = current + timedelta(seconds=600)
    return {
        "enabled": True,
        "status": "SCHEDULED",
        "runtime_role": "WEB",
        "execution_profile": "CHALLENGE_1_V2",
        "interval_seconds": 600,
        "search_continues": True,
        "next_cycle_scheduled": True,
        "next_cycle_at_utc": next_cycle.isoformat(),
        "next_cycle_in_seconds": 600,
        "last_message_fa": "زمان‌بند Challenge 1 V2 در نقش WEB فعال شد؛ نخستین چرخه پس از ۶۰۰ ثانیه اجرا می‌شود.",
    }


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
