"""Small, process-local cache and freshness helpers for the read-only Demo dashboard."""
from __future__ import annotations

import copy
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

_CACHE_LOCK = threading.RLock()
_CACHE: dict[Any, tuple[float, dict[str, Any]]] = {}


def cached_demo_dashboard_report(
    loader: Callable[[], dict[str, Any]], *, ttl_seconds: int = 60,
    now: float | None = None,
) -> dict[str, Any]:
    """Cache the authoritative Turso report for at most 60 seconds."""
    current = time.monotonic() if now is None else float(now)
    with _CACHE_LOCK:
        cached = _CACHE.get(loader)
        if cached is not None and cached[0] > current:
            return copy.deepcopy(cached[1])
        fresh = loader()
        if not isinstance(fresh, dict):
            fresh = {"status": "UNAVAILABLE", "read_only": True, "orders_submitted": False}
        _CACHE[loader] = (current + max(1, int(ttl_seconds)), copy.deepcopy(fresh))
        return copy.deepcopy(fresh)


def cycle_lag_status(last_successful_cycle_at: Any, *, now: datetime | None = None,
                     threshold_minutes: int = 25) -> dict[str, Any]:
    """Describe last successful Turso cycle age; missing/invalid timestamps stay unknown."""
    raw = str(last_successful_cycle_at or "").strip()
    if not raw:
        return {
            "last_successful_cycle_at": None,
            "cycle_age_minutes": None,
            "cycle_lag_warning": True,
            "cycle_lag_status": "NO_SUCCESSFUL_CYCLE",
            "cycle_lag_message_fa": "هشدار: زمان آخرین چرخه موفق در Turso ثبت نشده است.",
        }
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        current = now or datetime.now(timezone.utc)
        age_seconds = max(0.0, (current.astimezone(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return {
            "last_successful_cycle_at": raw,
            "cycle_age_minutes": None,
            "cycle_lag_warning": True,
            "cycle_lag_status": "INVALID_CYCLE_TIMESTAMP",
            "cycle_lag_message_fa": "هشدار: زمان آخرین چرخه موفق در Turso معتبر نیست.",
        }
    age_minutes = round(age_seconds / 60.0, 2)
    warning = age_seconds > int(threshold_minutes) * 60
    return {
        "last_successful_cycle_at": parsed.astimezone(timezone.utc).isoformat(),
        "cycle_age_minutes": age_minutes,
        "cycle_lag_warning": warning,
        "cycle_lag_status": "DELAYED" if warning else "CURRENT",
        "cycle_lag_message_fa": (
            f"هشدار تأخیر چرخه: {age_minutes:.1f} دقیقه از آخرین چرخه موفق گذشته است."
            if warning else f"آخرین چرخه موفق {age_minutes:.1f} دقیقه پیش ثبت شده است."
        ),
    }
