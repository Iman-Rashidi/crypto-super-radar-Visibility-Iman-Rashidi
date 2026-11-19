"""Durable, observation-only AI review scheduling for open BloFin Demo positions.

The position manager keeps its 15-second deterministic risk loop. Independent AI
reviews are scheduled at a much slower, configurable cadence so free-provider
budgets are not exhausted. Every manager cycle records each reviewer's state,
including explicit NOT_CALLED reasons.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Mapping

from .ai_second_opinion import run_challenge1_observer_reviews
from .challenge1_audit import Challenge1AuditRecorder
from .challenge1_supervisor_policy import coordinate_position_supervisors
from .notion_event_sync import NotionEventOutbox


_REVIEW_THREADS: dict[str, threading.Thread] = {}
_REVIEW_LOCK = threading.RLock()
_DEFAULT_INTERVAL_SECONDS = 3_600
_IN_FLIGHT_TIMEOUT_SECONDS = 90
_RETRY_AFTER_INTERRUPTED_SECONDS = 900
_MAX_AUDIT_EVENTS = 2_000


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _audit_details(event: Mapping[str, Any]) -> dict[str, Any]:
    payload = event.get("payload")
    payload = payload if isinstance(payload, Mapping) else {}
    details = payload.get("details")
    return dict(details) if isinstance(details, Mapping) else {}

def _material_review_event_key(
    challenge_record: Mapping[str, Any],
    cycle_context: Mapping[str, Any],
    position_id: str,
) -> str:
    """Hash material state changes without reacting to every mark-price tick."""
    position = cycle_context.get("position")
    position = position if isinstance(position, Mapping) else {}
    protective = cycle_context.get("protective_orders")
    protective = protective if isinstance(protective, Mapping) else {}
    candles = cycle_context.get("market_direction_evidence")
    candles = candles if isinstance(candles, list) else []
    last_candle = candles[-1] if candles else None
    if isinstance(last_candle, Mapping):
        candle_marker = next(
            (last_candle.get(key) for key in ("ts", "timestamp", "time", "open_time", "openTime")
             if last_candle.get(key) not in (None, "")),
            None,
        )
    elif isinstance(last_candle, (list, tuple)) and last_candle:
        candle_marker = last_candle[0]
    else:
        candle_marker = None
    raw_evidence = challenge_record.get("challenge1_evidence")
    raw_evidence = raw_evidence if isinstance(raw_evidence, Mapping) else {}
    protective_orders = protective.get("orders")
    protective_orders = protective_orders if isinstance(protective_orders, list) else []
    protection_signature = [
        {
            "tpsl_id": item.get("tpsl_id"),
            "position_id": item.get("position_id"),
            "state": item.get("state"),
            "sl_trigger_price": item.get("sl_trigger_price"),
            "tp_trigger_price": item.get("tp_trigger_price"),
        }
        for item in protective_orders if isinstance(item, Mapping)
    ]
    position_signature = {
        "position_id": str(position.get("positionId") or position_id),
        "instrument_id": str(position.get("instId") or challenge_record.get("instrument_id") or ""),
        "side": str(position.get("positionSide") or ""),
        "size": str(position.get("positions") or ""),
        "average_price": str(position.get("averagePrice") or ""),
        "liquidation_price": str(position.get("liquidationPrice") or ""),
        "unrealized_pnl_sign": (
            "NEGATIVE" if _is_negative_number(position.get("unrealizedPnl"))
            else "NON_NEGATIVE" if _is_finite_number(position.get("unrealizedPnl"))
            else "UNKNOWN"
        ),
    }
    material = {
        "position": position_signature,
        "robot_decision": str(cycle_context.get("robot_decision") or "UNKNOWN"),
        "market_direction": str(cycle_context.get("market_direction") or "UNKNOWN"),
        "last_closed_candle_marker": candle_marker,
        "setup_pattern": raw_evidence.get("chosen_pattern"),
        "chart_structure": raw_evidence.get("chart_structure"),
        "protection_status": protective.get("status"),
        "protection_orders": protection_signature,
    }
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _is_finite_number(value: Any) -> bool:
    try:
        number = float(value)
        return number == number and number not in (float("inf"), float("-inf"))
    except (TypeError, ValueError, OverflowError):
        return False


def _is_negative_number(value: Any) -> bool:
    try:
        return _is_finite_number(value) and float(value) < 0
    except (TypeError, ValueError, OverflowError):
        return False


def record_position_supervisor_watchdog_cycle(
    *,
    experience_id: str | None,
    position_id: str,
    cycle_epoch_ms: int,
    snapshot_age_seconds: float,
    snapshot_source: str,
    snapshot_status: str,
    reviewer_states: Mapping[str, Any],
    deterministic_flags: list[str] | None = None,
    loop_drift_ms: int | None = None,
    connection: Any = None,
) -> dict[str, Any]:
    """Persist one compact 3-second local supervisor-coordinator heartbeat.

    This function makes no network, LLM, Notion, or exchange calls.
    """
    if not position_id:
        raise ValueError("position_id is required for supervisor watchdog audit.")
    cycle_id = f"{position_id}:{int(cycle_epoch_ms)}"
    details = {
        "environment": "BLOFIN_DEMO",
        "watchdog_interval_seconds": 3,
        "cycle_epoch_ms": int(cycle_epoch_ms),
        "snapshot_age_seconds": round(max(0.0, float(snapshot_age_seconds)), 3),
        "snapshot_source": str(snapshot_source or "UNKNOWN")[:80],
        "snapshot_status": str(snapshot_status or "UNKNOWN")[:80],
        "reviewer_states": {
            reviewer_id: {
                "status": str((reviewer_states.get(reviewer_id) or {}).get("status") or "UNKNOWN"),
                "action": str((reviewer_states.get(reviewer_id) or {}).get("action") or ""),
                "last_action": str((reviewer_states.get(reviewer_id) or {}).get("last_action") or ""),
                "last_result_age_seconds": (reviewer_states.get(reviewer_id) or {}).get("last_result_age_seconds"),
                "reason_code": str((reviewer_states.get(reviewer_id) or {}).get("reason_code") or "")[:100],
            }
            for reviewer_id in ("nemotron", "second_ai")
            if isinstance(reviewer_states.get(reviewer_id), Mapping)
        },
        "deterministic_flags": [str(flag)[:100] for flag in (deterministic_flags or [])[:12]],
        "loop_drift_ms": int(loop_drift_ms) if loop_drift_ms is not None else None,
        "llm_request_attempted": False,
        "rest_request_attempted": False,
        "exchange_write_attempted": False,
        "execution_effect": "NONE",
    }
    return _record_audit_event(
        cycle_id=experience_id or f"unlinked-position:{position_id}",
        phase="POSITION_SUPERVISOR_WATCHDOG_CYCLE",
        event_key=f"position-supervisor-watchdog:{position_id}:{cycle_id}",
        details=details,
        experience_id=experience_id,
        position_id=position_id,
        connection=connection,
    )


def _record_audit_event(
    *,
    cycle_id: str,
    phase: str,
    event_key: str,
    details: Mapping[str, Any],
    experience_id: str | None,
    position_id: str,
    connection: Any = None,
) -> dict[str, Any]:
    recorder = Challenge1AuditRecorder(connection=connection) if connection is not None else Challenge1AuditRecorder()
    try:
        return recorder.append_event(
            cycle_id=cycle_id,
            phase=phase,
            event_key=event_key,
            details=dict(details),
            experience_id=experience_id or None,
            position_id=position_id or None,
        )
    finally:
        recorder.close()


def _queue_notion_projection(
    saved: Mapping[str, Any],
    *,
    event_type: str,
    details: Mapping[str, Any],
    experience_id: str,
    position_id: str,
    order_id: str,
    symbol: str,
    signal: str,
    reviewer_id: str = "",
    provider: str = "",
    model: str = "",
    call_status: str = "",
    validation: str = "",
    requested_at: str = "",
    completed_at: str = "",
    consensus: str = "",
    reason: str = "",
) -> str:
    outbox = None
    try:
        outbox = NotionEventOutbox()
        outbox.enqueue({
            "event_id": str(saved.get("event_id") or ""),
            "cycle_id": str(saved.get("cycle_id") or ""),
            "occurred_at_utc": str(saved.get("occurred_at_utc") or _now_utc().isoformat()),
            "event_type": event_type,
            "actor": "Robot",
            "status": "SUCCESS" if call_status == "REVIEWED" else "WARNING",
            "market": "Futures",
            "symbol": symbol,
            "timeframe": "5m",
            "signal": signal,
            "ai_decision": str(details.get("action") or "ACTIVE_SUPERVISION"),
            "reviewer_id": reviewer_id,
            "ai_provider": provider,
            "ai_model": model,
            "ai_requested_at_utc": requested_at or "",
            "ai_completed_at_utc": completed_at or "",
            "ai_call_status": call_status or "",
            "ai_validation": validation or "NOT_RUN",
            "reviewer_consensus": consensus or "NOT_COMPARABLE",
            "input_data_type": "LIVE_MARKET",
            "ai_analysis": reason[:1600],
            "ai_suggestion": json.dumps(
                {
                    "observations": details.get("observations", []),
                    "risk_flags": details.get("risk_flags", []),
                    "action": details.get("action"),
                    "proposed_stop_loss_price": details.get("proposed_stop_loss_price"),
                    "data_gaps": details.get("data_gaps", []),
                },
                ensure_ascii=False,
                default=str,
            )[:1600],
            "reason": reason[:1600] or "POSITION_REVIEW_EVENT",
            "details": json.dumps(dict(details), ensure_ascii=False, sort_keys=True, default=str)[:1750],
            "experience_id": experience_id,
            "order_id": order_id,
            "position_id": position_id,
            "source_url": (
                os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
                + "/api/blofin/demo/audit/" + str(saved.get("cycle_id") or "")
                if os.getenv("RENDER_EXTERNAL_URL", "").strip() else ""
            ),
        })
        return "QUEUED"
    except Exception as exc:
        return "OUTBOX_FAILED:" + type(exc).__name__
    finally:
        if outbox is not None:
            outbox.close()


def _record_notion_projection_failure(
    *,
    saved: Mapping[str, Any],
    status: str,
    event_type: str,
    experience_id: str | None,
    position_id: str,
    connection: Any = None,
) -> dict[str, Any]:
    """Record failed outbox enqueue separately from the original audit event."""
    error_type = status.partition(":")[2] or "UNKNOWN"
    audit_reference = experience_id or f"unlinked-position:{position_id}"
    return _record_audit_event(
        cycle_id=audit_reference,
        phase="POSITION_NOTION_OUTBOX_FAILED",
        event_key=f"position-notion-outbox-failed:{saved.get('event_id')}:{event_type}",
        details={
            "source_event_id": str(saved.get("event_id") or ""),
            "event_type": event_type,
            "outbox_status": status,
            "error_type": error_type,
            "sync_status": "NOT_ENQUEUED",
            "execution_effect": "NONE",
        },
        experience_id=experience_id,
        position_id=position_id,
        connection=connection,
    )


def record_position_monitor_cycle(
    *,
    experience_id: str | None,
    position_id: str,
    order_id: str,
    symbol: str,
    signal: str,
    cycle_details: Mapping[str, Any],
    cycle_epoch_ms: int | None = None,
    connection: Any = None,
) -> dict[str, Any]:
    """Persist every monitor cycle in the hash chain, then summarize completed minutes to Notion."""
    if not position_id:
        raise ValueError("position_id is required for a position cycle.")
    audit_reference = experience_id or f"unlinked-position:{position_id}"
    epoch_ms = int(cycle_epoch_ms or time.time() * 1000)
    now_bucket = int(epoch_ms // 60_000)
    cycle_key = f"position-cycle:{position_id}:{epoch_ms}"
    saved = _record_audit_event(
        cycle_id=audit_reference,
        phase="POSITION_EXIT_EVALUATION",
        event_key=cycle_key,
        details={
            **dict(cycle_details),
            "monitor_cycle_epoch_ms": epoch_ms,
            "monitor_cycle_minute_bucket": now_bucket,
            "observer_recording": "PER_CYCLE",
        },
        experience_id=experience_id,
        position_id=position_id,
        connection=connection,
    )

    recorder = Challenge1AuditRecorder(connection=connection) if connection is not None else Challenge1AuditRecorder()
    summaries: list[dict[str, Any]] = []
    try:
        report = recorder.list_related(audit_reference, limit=_MAX_AUDIT_EVENTS)
        events = report.get("events") if isinstance(report.get("events"), list) else []
        cycles_by_minute: dict[int, list[dict[str, Any]]] = {}
        summarized: set[int] = set()
        for item in events:
            if not isinstance(item, Mapping):
                continue
            phase = str(item.get("phase") or "")
            details = _audit_details(item)
            if phase == "POSITION_EXIT_EVALUATION":
                bucket = details.get("monitor_cycle_minute_bucket")
                try:
                    bucket_int = int(bucket)
                except (TypeError, ValueError):
                    continue
                if bucket_int < now_bucket:
                    cycles_by_minute.setdefault(bucket_int, []).append(dict(item))
            elif phase == "POSITION_MANAGER_CYCLE_SUMMARY":
                try:
                    summarized.add(int(details.get("minute_bucket")))
                except (TypeError, ValueError):
                    pass

        for bucket in sorted(set(cycles_by_minute) - summarized):
            minute_events = cycles_by_minute[bucket]
            cycle_details_list = [_audit_details(item) for item in minute_events]
            last_cycle = cycle_details_list[-1] if cycle_details_list else {}
            decision_counts: dict[str, int] = {}
            reviewer_states: dict[str, dict[str, int]] = {}
            for detail in cycle_details_list:
                decision = str(detail.get("decision") or "UNKNOWN")
                decision_counts[decision] = decision_counts.get(decision, 0) + 1
                states = detail.get("reviewer_states")
                if isinstance(states, Mapping):
                    for reviewer_id, reviewer_state in states.items():
                        reviewer_state = reviewer_state if isinstance(reviewer_state, Mapping) else {}
                        state = str(reviewer_state.get("status") or "UNKNOWN")
                        per_reviewer = reviewer_states.setdefault(str(reviewer_id), {})
                        per_reviewer[state] = per_reviewer.get(state, 0) + 1

            summary_detail = {
                "instrument_id": symbol,
                "position_id": position_id,
                "experience_id": experience_id,
                "experience_link_status": "LINKED" if experience_id else "MISSING",
                "minute_bucket": bucket,
                "cycle_count": len(minute_events),
                "cycle_event_ids": [str(item.get("event_id") or "") for item in minute_events],
                "decision_counts": decision_counts,
                "reviewer_status_counts": reviewer_states,
                "last_cycle_snapshot": last_cycle,
                "audit_integrity_status": report.get("integrity", {}).get("status", "UNKNOWN"),
                "source_of_truth": "APPEND_ONLY_CHALLENGE1_AUDIT_CHAIN",
            }
            summary_saved = recorder.append_event(
                cycle_id=audit_reference,
                phase="POSITION_MANAGER_CYCLE_SUMMARY",
                event_key=f"position-cycle-summary:{position_id}:{bucket}",
                details=summary_detail,
                experience_id=experience_id,
                position_id=position_id,
            )
            notion_status = (
                _queue_notion_projection(
                    summary_saved,
                    event_type="POSITION_REVIEW",
                    details=summary_detail,
                    experience_id=experience_id,
                    position_id=position_id,
                    order_id=order_id,
                    symbol=symbol,
                    signal=signal,
                    call_status="CYCLE_SUMMARY",
                    validation="NOT_APPLICABLE",
                    reason=f"{len(minute_events)} position monitor cycles summarized for minute bucket {bucket}.",
                )
                if experience_id else "NOT_QUEUED_EXPERIENCE_ID_MISSING"
            )
            projection_failure_event = None
            if notion_status.startswith("OUTBOX_FAILED:"):
                try:
                    failed_event = _record_notion_projection_failure(
                        saved=summary_saved,
                        status=notion_status,
                        event_type="POSITION_REVIEW",
                        experience_id=experience_id,
                        position_id=position_id,
                        connection=connection,
                    )
                    projection_failure_event = failed_event.get("event_id")
                except Exception as audit_exc:
                    projection_failure_event = "AUDIT_FAILURE:" + type(audit_exc).__name__
            summaries.append({
                "minute_bucket": bucket,
                "event_id": summary_saved.get("event_id"),
                "notion_sync_status": notion_status,
                "notion_failure_audit_event_id": projection_failure_event,
            })
    finally:
        recorder.close()

    return {
        "status": "RECORDED",
        "event_id": saved.get("event_id"),
        "cycle_id": saved.get("cycle_id"),
        "experience_link_status": "LINKED" if experience_id else "MISSING",
        "event_hash": saved.get("event_hash"),
        "inserted": saved.get("inserted", False),
        "minute_summaries": summaries,
    }


def record_position_observer_skip(
    *,
    experience_id: str | None,
    position_id: str,
    reviewer_id: str,
    cycle_id: str,
    reason_code: str,
    reason: str,
    last_result: Mapping[str, Any] | None = None,
    connection: Any = None,
) -> dict[str, Any]:
    """Write a distinct append-only NOT_CALLED event for one reviewer in one manager cycle."""
    if not position_id or reviewer_id not in {"nemotron", "second_ai"}:
        raise ValueError("Valid position and reviewer identifiers are required.")
    audit_reference = experience_id or f"unlinked-position:{position_id}"
    details = {
        "reviewer_id": reviewer_id,
        "status": "NOT_CALLED",
        "request_attempted": False,
        "reason_code": str(reason_code or "UNKNOWN_SKIP_REASON")[:100],
        "reason": str(reason or "")[:500],
        "cycle_id": cycle_id,
        "execution_effect": "NONE",
        "last_result": dict(last_result or {}),
    }
    return _record_audit_event(
        cycle_id=audit_reference,
        phase="POSITION_AI_REVIEW_SKIPPED",
        event_key=f"position-review-skip:{position_id}:{cycle_id}:{reviewer_id}",
        details=details,
        experience_id=experience_id,
        position_id=position_id,
        connection=connection,
    )


def _reviewer_result_event(
    *,
    review_batch_id: str,
    reviewer: Mapping[str, Any],
    consensus: str,
    experience_id: str,
    position_id: str,
    order_id: str,
    symbol: str,
    signal: str,
) -> dict[str, Any]:
    result = dict(reviewer)
    reviewer_id = str(result.get("reviewer_id") or "unknown")
    status = str(result.get("status") or "UNKNOWN").upper()
    details = {
        "review_batch_id": review_batch_id,
        "reviewer_id": reviewer_id,
        "status": status,
        "request_attempted": result.get("request_attempted") if isinstance(result.get("request_attempted"), bool) else None,
        "provider": str(result.get("provider") or ""),
        "model": str(result.get("model") or ""),
        "returned_model": str(result.get("returned_model") or ""),
        "request_started_at_utc": str(result.get("request_started_at_utc") or result.get("requested_at_utc") or ""),
        "response_completed_at_utc": str(result.get("response_completed_at_utc") or result.get("completed_at_utc") or ""),
        "latency_ms": result.get("latency_ms"),
        "http_status": result.get("http_status"),
        "response_validated": result.get("response_validated") if isinstance(result.get("response_validated"), bool) else None,
        "model_verified": result.get("model_verified") if isinstance(result.get("model_verified"), bool) else None,
        "verdict": str(result.get("verdict") or ""),
        "action": str(result.get("action") or ""),
        "proposed_stop_loss_price": result.get("proposed_stop_loss_price"),
        "data_gaps": list(result.get("data_gaps") or [])[:10],
        "evidence_hash": str(result.get("evidence_hash") or ""),
        "mandate": str(result.get("mandate") or ""),
        "reason_fa": str(result.get("reason_fa") or result.get("reason") or "")[:1000],
        "observations": list(result.get("observations") or [])[:10],
        "risk_flags": list(result.get("risk_flags") or [])[:10],
        "skip_reason_code": str(result.get("skip_reason_code") or result.get("error_code") or ""),
        "configuration_issues": list(result.get("configuration_issues") or [])[:10],
        "execution_effect": str(result.get("execution_effect") or "NONE"),
        "observation_only": False,
    }
    phase = "POSITION_AI_REVIEW_RESULT"
    event_key = f"position-review-result:{position_id}:{review_batch_id}:{reviewer_id}"
    saved = _record_audit_event(
        cycle_id=experience_id,
        phase=phase,
        event_key=event_key,
        details=details,
        experience_id=experience_id,
        position_id=position_id,
    )
    validation = "VALIDATED" if details["response_validated"] and details["model_verified"] else "NOT_VALIDATED"
    notion_status = _queue_notion_projection(
        saved,
        event_type="POSITION_REVIEW",
        details=details,
        experience_id=experience_id,
        position_id=position_id,
        order_id=order_id,
        symbol=symbol,
        signal=signal,
        reviewer_id=reviewer_id,
        provider=details["provider"],
        model=details["returned_model"] or details["model"],
        call_status=status,
        validation=validation,
        requested_at=details["request_started_at_utc"],
        completed_at=details["response_completed_at_utc"],
        consensus=consensus,
        reason=details["reason_fa"] or details["skip_reason_code"],
    )
    notion_failure_event_id = None
    if notion_status.startswith("OUTBOX_FAILED:"):
        try:
            failed_event = _record_notion_projection_failure(
                saved=saved,
                status=notion_status,
                event_type="POSITION_REVIEW",
                experience_id=experience_id,
                position_id=position_id,
            )
            notion_failure_event_id = failed_event.get("event_id")
        except Exception as audit_exc:
            notion_failure_event_id = "AUDIT_FAILURE:" + type(audit_exc).__name__
    return {
        "event_id": saved.get("event_id"),
        "notion_sync_status": notion_status,
        "notion_failure_audit_event_id": notion_failure_event_id,
        **details,
    }


def _position_review_worker(
    *,
    review_batch_id: str,
    challenge_record: Mapping[str, Any],
    cycle_context: Mapping[str, Any],
    experience_id: str,
    position_id: str,
    order_id: str,
    symbol: str,
    signal: str,
    action_executor: Any = None,
) -> None:
    try:
        from .ai_second_opinion import run_challenge1_observer_reviews
        result = run_challenge1_observer_reviews(challenge_record, cycle_context)
        reviewers = result.get("reviewers") if isinstance(result.get("reviewers"), list) else []
        consensus = str(result.get("consensus") or "NOT_COMPARABLE")
        seen = set()
        for reviewer in reviewers:
            if not isinstance(reviewer, Mapping):
                continue
            reviewer_id = str(reviewer.get("reviewer_id") or "")
            if reviewer_id not in {"nemotron", "second_ai"} or reviewer_id in seen:
                continue
            seen.add(reviewer_id)
            _reviewer_result_event(
                review_batch_id=review_batch_id,
                reviewer=reviewer,
                consensus=consensus,
                experience_id=experience_id,
                position_id=position_id,
                order_id=order_id,
                symbol=symbol,
                signal=signal,
            )
        for reviewer_id in {"nemotron", "second_ai"} - seen:
            _reviewer_result_event(
                review_batch_id=review_batch_id,
                reviewer={
                    "reviewer_id": reviewer_id,
                    "status": "OBSERVER_RESULT_MISSING",
                    "request_attempted": False,
                    "skip_reason_code": "OBSERVER_RESULT_MISSING_FROM_BATCH",
                    "reason": "The observer batch returned no result for this reviewer.",
                },
                consensus=consensus,
                experience_id=experience_id,
                position_id=position_id,
                order_id=order_id,
                symbol=symbol,
                signal=signal,
            )

        position = cycle_context.get("position")
        position = position if isinstance(position, Mapping) else {}
        cycle_context = {
            **dict(cycle_context),
            "supervisor_reviews": [dict(item) for item in reviewers if isinstance(item, Mapping)],
        }
        evidence_hash = str(result.get("evidence_hash") or "")
        coordinator = coordinate_position_supervisors(
            reviewers,
            evidence_hash=evidence_hash,
            experience_id=experience_id,
            position_id=position_id,
            current_position_id=str(position.get("positionId") or ""),
        )
        coordinator_event = _record_audit_event(
            cycle_id=experience_id,
            phase="POSITION_AI_COORDINATOR_DECISION",
            event_key=f"position-supervisor-coordinator:{position_id}:{review_batch_id}",
            details={
                **coordinator,
                "review_batch_id": review_batch_id,
                "reviewer_consensus": consensus,
                "supervisor_actions": {
                    str(item.get("reviewer_id") or ""): str(item.get("action") or "")
                    for item in reviewers if isinstance(item, Mapping)
                },
                "decided_at_utc": _now_utc().isoformat(),
            },
            experience_id=experience_id,
            position_id=position_id,
        )
        execution_result: dict[str, Any] = {
            "status": "NOT_EXECUTED",
            "reason_code": "NO_AUTHORIZED_ACTION",
        }
        if coordinator.get("execution_authorized") is True:
            if callable(action_executor):
                try:
                    raw_execution = action_executor(
                        action=str(coordinator.get("action") or ""),
                        challenge_record=dict(challenge_record),
                        cycle_context=dict(cycle_context),
                        coordinator_decision=dict(coordinator),
                    )
                    execution_result = (
                        dict(raw_execution) if isinstance(raw_execution, Mapping)
                        else {"status": "UNKNOWN", "reason_code": "EXECUTOR_RETURN_NOT_MAPPING"}
                    )
                except Exception as action_exc:
                    execution_result = {
                        "status": "EXECUTION_ERROR",
                        "reason_code": type(action_exc).__name__,
                    }
            else:
                execution_result = {
                    "status": "BLOCKED",
                    "reason_code": "DEMO_ACTION_EXECUTOR_NOT_CONFIGURED",
                }
        elif coordinator.get("status") == "RISK_ALERT":
            if callable(action_executor):
                try:
                    raw_execution = action_executor(
                        action="URGENT_RISK_ALERT",
                        challenge_record=dict(challenge_record),
                        cycle_context=dict(cycle_context),
                        coordinator_decision=dict(coordinator),
                    )
                    execution_result = (
                        dict(raw_execution) if isinstance(raw_execution, Mapping)
                        else {"status": "ALERT_RECORDED", "reason_code": "NO_ACTION_EXECUTOR_RESULT"}
                    )
                except Exception as action_exc:
                    execution_result = {"status": "ALERT_EXECUTION_ERROR", "reason_code": type(action_exc).__name__}
            else:
                execution_result = {"status": "ALERT_RECORDED", "reason_code": "NO_ALERT_HANDLER_CONFIGURED"}
        _record_audit_event(
            cycle_id=experience_id,
            phase="POSITION_AI_INTERVENTION_EXECUTION",
            event_key=f"position-supervisor-execution:{position_id}:{review_batch_id}",
            details={
                "review_batch_id": review_batch_id,
                "coordinator_event_id": coordinator_event.get("event_id"),
                "coordinator_status": coordinator.get("status"),
                "action": coordinator.get("action"),
                "execution_authorized": coordinator.get("execution_authorized") is True,
                "execution_result": execution_result,
                "execution_effect": execution_result.get("status", "UNKNOWN"),
                "recorded_at_utc": _now_utc().isoformat(),
            },
            experience_id=experience_id,
            position_id=position_id,
        )
        _record_audit_event(
            cycle_id=experience_id,
            phase="POSITION_AI_REVIEW_BATCH_COMPLETE",
            event_key=f"position-review-batch-complete:{position_id}:{review_batch_id}",
            details={
                "review_batch_id": review_batch_id,
                "consensus": consensus,
                "reviewers_recorded": sorted(seen),
                "execution_effect": str(execution_result.get("status") or "UNKNOWN"),
                "coordinator_status": coordinator.get("status"),
                "completed_at_utc": _now_utc().isoformat(),
            },
            experience_id=experience_id,
            position_id=position_id,
        )
    except Exception as exc:
        for reviewer_id in ("nemotron", "second_ai"):
            try:
                _reviewer_result_event(
                    review_batch_id=review_batch_id,
                    reviewer={
                        "reviewer_id": reviewer_id,
                        "status": "OBSERVER_WORKER_FAILED",
                        "request_attempted": None,
                        "skip_reason_code": "OBSERVER_WORKER_FAILED",
                        "error_type": type(exc).__name__,
                        "reason": "The worker failed; whether a provider request reached the provider is unknown.",
                    },
                    consensus="NOT_COMPARABLE",
                    experience_id=experience_id,
                    position_id=position_id,
                    order_id=order_id,
                    symbol=symbol,
                    signal=signal,
                )
            except Exception:
                pass
    finally:
        with _REVIEW_LOCK:
            _REVIEW_THREADS.pop(position_id, None)


def schedule_position_observer_review(
    *,
    challenge_record: Mapping[str, Any],
    cycle_context: Mapping[str, Any],
    experience_id: str,
    position_id: str,
    order_id: str,
    symbol: str,
    signal: str,
    now: datetime | None = None,
    action_executor: Any = None,
) -> dict[str, Any]:
    """Start two independent active reviewers; every result and execution is durably audited."""
    current = (now or _now_utc()).astimezone(timezone.utc)
    now_epoch = current.timestamp()
    try:
        interval_seconds = int(os.getenv(
            "BLOFIN_DEMO_POSITION_AI_REVIEW_INTERVAL_SECONDS",
            str(_DEFAULT_INTERVAL_SECONDS),
        ))
    except (TypeError, ValueError):
        interval_seconds = _DEFAULT_INTERVAL_SECONDS
    interval_seconds = max(3_600, min(interval_seconds, 86_400))

    if not experience_id or not position_id:
        return {
            key: {
                "status": "NOT_CALLED",
                "reason_code": "EXPERIENCE_OR_POSITION_ID_MISSING",
                "request_attempted": False,
            }
            for key in ("nemotron", "second_ai")
        }

    recorder = Challenge1AuditRecorder()
    try:
        report = recorder.list_related(experience_id, limit=_MAX_AUDIT_EVENTS)
        events = report.get("events") if isinstance(report.get("events"), list) else []
    finally:
        recorder.close()

    latest_request = None
    for event in events:
        if str(event.get("phase") or "") == "POSITION_AI_REVIEW_BATCH_REQUESTED":
            latest_request = event
    with _REVIEW_LOCK:
        active_thread = _REVIEW_THREADS.get(position_id)
        if active_thread is not None and active_thread.is_alive():
            return {
                key: {
                    "status": "PENDING",
                    "reason_code": "REVIEW_BATCH_IN_FLIGHT",
                    "request_attempted": False,
                }
                for key in ("nemotron", "second_ai")
            }

    latest_batch_id = ""
    latest_request_time = None
    latest_details: dict[str, Any] = {}
    latest_results: dict[str, dict[str, Any]] = {}
    current_material_hash = _material_review_event_key(
        challenge_record, cycle_context, position_id,
    )
    if latest_request is not None:
        latest_details = _audit_details(latest_request)
        latest_batch_id = str(latest_details.get("review_batch_id") or "")
        latest_request_time = _parse_utc(latest_request.get("occurred_at_utc"))
        for event in events:
            if str(event.get("phase") or "") != "POSITION_AI_REVIEW_RESULT":
                continue
            details = _audit_details(event)
            if str(details.get("review_batch_id") or "") != latest_batch_id:
                continue
            reviewer_id = str(details.get("reviewer_id") or "")
            if reviewer_id in {"nemotron", "second_ai"}:
                latest_results[reviewer_id] = {"event_id": event.get("event_id"), **details}

    incomplete_batch = len(latest_results) < 2 or any(
        str(item.get("status") or "").upper() in {"OBSERVER_WORKER_FAILED", "OBSERVER_RESULT_MISSING"}
        for item in latest_results.values()
    )
    if latest_request is not None and incomplete_batch:
        age = now_epoch - latest_request_time.timestamp() if latest_request_time else float("inf")
        if age < _IN_FLIGHT_TIMEOUT_SECONDS:
            return {
                key: {
                    "status": "PENDING",
                    "reason_code": "REVIEW_BATCH_WAITING_FOR_RESULTS",
                    "request_attempted": False,
                    "review_batch_id": latest_batch_id,
                }
                for key in ("nemotron", "second_ai")
            }
        recovered_before = any(
            str(event.get("phase") or "") == "POSITION_AI_REVIEW_RECOVERY"
            and str(_audit_details(event).get("review_batch_id") or "") == latest_batch_id
            for event in events
        )
        if not recovered_before:
            _record_audit_event(
                cycle_id=experience_id,
                phase="POSITION_AI_REVIEW_RECOVERY",
                event_key=f"position-review-recovery:{position_id}:{latest_batch_id}",
                details={
                    "review_batch_id": latest_batch_id,
                    "status": "UNKNOWN",
                    "reason_code": "OBSERVER_RESULT_MISSING_AFTER_TIMEOUT_OR_RESTART",
                    "age_seconds": int(age) if age != float("inf") else None,
                    "execution_effect": "NONE",
                },
                experience_id=experience_id,
                position_id=position_id,
            )
        recovery_age = age - _IN_FLIGHT_TIMEOUT_SECONDS
        if recovery_age < _RETRY_AFTER_INTERRUPTED_SECONDS:
            return {
                key: {
                    "status": "NOT_CALLED",
                    "reason_code": "OBSERVER_RESULT_MISSING_AFTER_TIMEOUT_OR_RESTART",
                    "request_attempted": False,
                    "review_batch_id": latest_batch_id,
                }
                for key in ("nemotron", "second_ai")
            }

    previous_material_hash = str(latest_details.get("material_event_hash") or "")
    material_event_changed = (
        latest_request is None
        or not previous_material_hash
        or previous_material_hash != current_material_hash
    )
    trigger_reason = "MATERIAL_POSITION_EVENT" if material_event_changed else "SCHEDULED_INTERVAL"
    if latest_request is not None and not incomplete_batch and latest_request_time is not None and len(latest_results) == 2:
        completed_times = [
            _parse_utc(item.get("response_completed_at_utc"))
            for item in latest_results.values()
        ]
        completed_times = [item for item in completed_times if item is not None]
        anchor = max(completed_times).timestamp() if completed_times else latest_request_time.timestamp()
        if now_epoch - anchor < interval_seconds and not material_event_changed:
            output: dict[str, Any] = {}
            for reviewer_id in ("nemotron", "second_ai"):
                previous = latest_results.get(reviewer_id, {})
                output[reviewer_id] = {
                    "status": "NOT_CALLED",
                    "reason_code": "SCHEDULED_INTERVAL_NOT_REACHED",
                    "request_attempted": False,
                    "last_result_status": previous.get("status"),
                    "last_verdict": previous.get("verdict"),
                    "last_action": previous.get("action"),
                    "last_result_event_id": previous.get("event_id"),
                    "last_result_age_seconds": max(0, int(now_epoch - anchor)),
                    "material_event_hash": current_material_hash,
                }
            return output

    review_batch_id = f"{position_id}:{int(now_epoch * 1000)}"
    _record_audit_event(
        phase="POSITION_AI_REVIEW_BATCH_REQUESTED",
        event_key=f"position-review-batch-requested:{position_id}:{review_batch_id}",
        details={
            "review_batch_id": review_batch_id,
            "reviewer_ids": ["nemotron", "second_ai"],
            "status": "REQUESTED",
            "request_attempted": True,
            "request_started_at_utc": current.isoformat(),
            "schedule_interval_seconds": interval_seconds,
            "material_event_hash": current_material_hash,
            "trigger_reason": trigger_reason,
            "robot_decision": str(cycle_context.get("robot_decision") or "UNKNOWN"),
            "execution_effect": "PENDING_COORDINATOR",
            "observation_only": False,
        },
        experience_id=experience_id,
        position_id=position_id,
        cycle_id=experience_id,
    )
    thread = threading.Thread(
        target=_position_review_worker,
        kwargs={
            "review_batch_id": review_batch_id,
            "challenge_record": dict(challenge_record),
            "cycle_context": dict(cycle_context),
            "experience_id": experience_id,
            "position_id": position_id,
            "order_id": order_id,
            "symbol": symbol,
            "signal": signal,
            "action_executor": action_executor,
        },
        name=f"position-ai-review-{position_id[:12]}",
        daemon=True,
    )
    with _REVIEW_LOCK:
        _REVIEW_THREADS[position_id] = thread
    thread.start()
    return {
        key: {
            "status": "REQUESTED",
            "reason_code": "SCHEDULED_POSITION_REVIEW",
            "request_attempted": True,
            "review_batch_id": review_batch_id,
        }
        for key in ("nemotron", "second_ai")
    }
