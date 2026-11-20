"""Five-minute, two-round Challenge 1 pre-entry evaluation policy.

This module deliberately does not call an AI provider or an exchange. It
provides deterministic timing and revalidation checks for the Demo entry path.
"""
from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

MIN_ENTRY_EVALUATION_SECONDS = 300
ENTRY_EVALUATION_HEARTBEAT_SECONDS = 3.0


class EntryEvaluationCancelled(RuntimeError):
    """The pending entry evaluation was cancelled before authorization."""


def _parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def wait_for_minimum_entry_evaluation_window(
    started_monotonic: float,
    *,
    cancel_check: Callable[[], bool] | None = None,
    progress: Callable[[float], None] | None = None,
    monotonic: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> float:
    """Wait until at least 300s have elapsed, checking local state every 3s.

    The heartbeat is local only: it does not call an LLM or poll the exchange.
    A fresh closed-candle analysis and a second independent AI review round
    must be performed by the caller after this wait.
    """
    clock = monotonic or time.monotonic
    sleeper = sleep or time.sleep
    if isinstance(started_monotonic, bool) or not isinstance(started_monotonic, (int, float)):
        raise ValueError("started_monotonic must be a finite monotonic timestamp")
    if not math.isfinite(float(started_monotonic)):
        raise ValueError("started_monotonic must be a finite monotonic timestamp")

    while True:
        if cancel_check is not None and cancel_check():
            raise EntryEvaluationCancelled("Challenge 1 entry evaluation was cancelled.")
        elapsed = max(0.0, float(clock()) - float(started_monotonic))
        if progress is not None:
            progress(elapsed)
        remaining = MIN_ENTRY_EVALUATION_SECONDS - elapsed
        if remaining <= 0:
            break
        sleeper(min(ENTRY_EVALUATION_HEARTBEAT_SECONDS, remaining))

    if cancel_check is not None and cancel_check():
        raise EntryEvaluationCancelled("Challenge 1 entry evaluation was cancelled.")
    return max(0.0, float(clock()) - float(started_monotonic))


def validate_challenge1_entry_revalidation(
    initial_record: Mapping[str, Any],
    refreshed_record: Mapping[str, Any],
    *,
    elapsed_seconds: float,
) -> dict[str, Any]:
    """Require the same setup to survive 5m and a newer completed 5m candle."""
    reasons: list[str] = []

    try:
        elapsed = float(elapsed_seconds)
    except (TypeError, ValueError, OverflowError):
        elapsed = 0.0
    if not math.isfinite(elapsed) or elapsed < MIN_ENTRY_EVALUATION_SECONDS:
        reasons.append("MINIMUM_EVALUATION_WINDOW_NOT_MET")

    initial_execution = initial_record.get("execution")
    initial_execution = initial_execution if isinstance(initial_execution, Mapping) else {}
    final_execution = refreshed_record.get("execution")
    final_execution = final_execution if isinstance(final_execution, Mapping) else {}
    initial_evidence = initial_record.get("challenge1_evidence")
    initial_evidence = initial_evidence if isinstance(initial_evidence, Mapping) else {}
    final_evidence = refreshed_record.get("challenge1_evidence")
    final_evidence = final_evidence if isinstance(final_evidence, Mapping) else {}
    initial_pattern = initial_evidence.get("chosen_pattern")
    initial_pattern = initial_pattern if isinstance(initial_pattern, Mapping) else {}
    final_pattern = final_evidence.get("chosen_pattern")
    final_pattern = final_pattern if isinstance(final_pattern, Mapping) else {}

    initial_signal = str(initial_record.get("signal") or "").strip().upper()
    final_signal = str(refreshed_record.get("signal") or "").strip().upper()
    initial_instrument = str(
        initial_record.get("instrument_id") or initial_record.get("inst_id") or ""
    ).strip().upper()
    final_instrument = str(
        refreshed_record.get("instrument_id") or refreshed_record.get("inst_id") or ""
    ).strip().upper()
    initial_signal_id = str(initial_record.get("signal_id") or "").strip()
    final_signal_id = str(refreshed_record.get("signal_id") or "").strip()
    initial_bar_label = str(
        initial_record.get("signal_bar_open_utc")
        or initial_execution.get("signal_bar_open_utc")
        or ""
    ).strip()
    final_bar_label = str(
        refreshed_record.get("signal_bar_open_utc")
        or final_execution.get("signal_bar_open_utc")
        or ""
    ).strip()
    initial_bar = _parse_utc(initial_bar_label)
    final_bar = _parse_utc(final_bar_label)

    if initial_signal not in {"LONG", "SHORT"} or final_signal != initial_signal:
        reasons.append("DIRECTION_NOT_PRESERVED")
    if not initial_instrument or final_instrument != initial_instrument:
        reasons.append("INSTRUMENT_CHANGED")
    if not initial_signal_id or not final_signal_id or final_signal_id == initial_signal_id:
        reasons.append("NEW_SIGNAL_ID_REQUIRED")
    if initial_execution.get("status") != "PASS" or final_execution.get("status") != "PASS":
        reasons.append("ENTRY_SETUP_NOT_VALID_ON_BOTH_ROUNDS")
    candle_advance_seconds = (
        (final_bar - initial_bar).total_seconds()
        if initial_bar is not None and final_bar is not None
        else None
    )
    if candle_advance_seconds is None or candle_advance_seconds < 300:
        reasons.append("NEWER_CLOSED_5M_CANDLE_NOT_CONFIRMED")
    if (
        initial_evidence.get("setup_timeframe") != "5m"
        or final_evidence.get("setup_timeframe") != "5m"
        or initial_evidence.get("completed_candles_only") is not True
        or final_evidence.get("completed_candles_only") is not True
    ):
        reasons.append("CLOSED_5M_CANDLE_EVIDENCE_REQUIRED")

    initial_pattern_name = str(initial_pattern.get("name_en") or "").strip()
    final_pattern_name = str(final_pattern.get("name_en") or "").strip()
    initial_pattern_direction = str(initial_pattern.get("direction") or "").strip().upper()
    final_pattern_direction = str(final_pattern.get("direction") or "").strip().upper()
    initial_pattern_category = str(initial_pattern.get("category") or "").strip().upper()
    final_pattern_category = str(final_pattern.get("category") or "").strip().upper()
    expected_pattern_direction = "BULLISH" if initial_signal == "LONG" else "BEARISH"
    if (
        not initial_pattern_name
        or final_pattern_name != initial_pattern_name
        or initial_pattern_direction != expected_pattern_direction
        or final_pattern_direction != initial_pattern_direction
        or final_pattern_category != initial_pattern_category
    ):
        reasons.append("SELECTED_PATTERN_NOT_PRESERVED")

    return {
        "status": "PASS" if not reasons else "BLOCKED",
        "reason_codes": reasons,
        "minimum_window_seconds": MIN_ENTRY_EVALUATION_SECONDS,
        "elapsed_seconds": round(elapsed, 3) if math.isfinite(elapsed) else None,
        "initial_signal_id": initial_signal_id or None,
        "final_signal_id": final_signal_id or None,
        "initial_signal_bar_open_utc": initial_bar_label or None,
        "final_signal_bar_open_utc": final_bar_label or None,
        "initial_direction": initial_signal or None,
        "final_direction": final_signal or None,
        "initial_pattern_name": initial_pattern_name or None,
        "final_pattern_name": final_pattern_name or None,
        "initial_pattern_direction": initial_pattern_direction or None,
        "final_pattern_direction": final_pattern_direction or None,
        "initial_pattern_category": initial_pattern_category or None,
        "final_pattern_category": final_pattern_category or None,
        "direction_preserved": initial_signal in {"LONG", "SHORT"} and final_signal == initial_signal,
        "pattern_preserved": (
            bool(initial_pattern_name)
            and final_pattern_name == initial_pattern_name
            and initial_pattern_direction == expected_pattern_direction
            and final_pattern_direction == initial_pattern_direction
            and final_pattern_category == initial_pattern_category
        ),
        "new_closed_candle_confirmed": bool(
            candle_advance_seconds is not None and candle_advance_seconds >= 300
            and initial_signal_id and final_signal_id and final_signal_id != initial_signal_id
        ),
        "candle_advance_seconds": candle_advance_seconds,
    }
