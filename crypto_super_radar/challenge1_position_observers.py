"""Durable, observation-only AI review scheduling for open BloFin Demo positions.

The position manager keeps its 15-second deterministic risk loop. Independent AI
reviews are scheduled at a much slower, configurable cadence so free-provider
budgets are not exhausted. Every manager cycle records each reviewer's state,
including explicit NOT_CALLED reasons.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Mapping

from .ai_second_opinion import run_challenge1_observer_reviews
from .challenge1_audit import Challenge1AuditRecorder
from .notion_event_sync import NotionEventOutbox


_REVIEW_THREADS: dict[str, threading.Thread] = {}
_REVIEW_LOCK = threading.RLock()
_DEFAULT_INTERVAL_SECONDS = 10_800
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


def _record_audit_event(
    *,
    cycle_id: str,
    phase: str,
    event_key: str,
    details: Mapping[str, Any],
    experience_id: str,
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
            "ai_decision": "OBSERVE_ONLY",
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


def record_position_monitor_cycle(
    *,
    experience_id: str,
    position_id: str,
    order_id: str,
    symbol: str,
    signal: str,
    cycle_details: Mapping[str, Any],
    cycle_epoch_ms: int | None = None,
) -> dict[str, Any]:
    """Persist every monitor cycle in the hash chain, then summarize completed minutes to Notion."""
    if not experience_id or not position_id:
        raise ValueError("experience_id and position_id are required for a position cycle.")
    epoch_ms = int(cycle_epoch_ms or time.time() * 1000)
    now_bucket = int(epoch_ms // 60_000)
    cycle_key = f"position-cycle:{position_id}:{epoch_ms}"
    saved = _record_audit_event(
        cycle_id=experience_id,
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
    )

    recorder = Challenge1AuditRecorder()
    summaries: list[dict[str, Any]] = []
    try:
        report = recorder.list_related(experience_id, limit=_MAX_AUDIT_EVENTS)
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
                cycle_id=experience_id,
                phase="POSITION_MANAGER_CYCLE_SUMMARY",
                event_key=f"position-cycle-summary:{position_id}:{bucket}",
                details=summary_detail,
                experience_id=experience_id,
                position_id=position_id,
            )
            notion_status = _queue_notion_projection(
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
            summaries.append({
                "minute_bucket": bucket,
                "event_id": summary_saved.get("event_id"),
                "notion_sync_status": notion_status,
            })
    finally:
        recorder.close()

    return {
        "status": "RECORDED",
        "event_id": saved.get("event_id"),
        "cycle_id": saved.get("cycle_id"),
        "event_hash": saved.get("event_hash"),
        "inserted": saved.get("inserted", False),
        "minute_summaries": summaries,
    }


def record_position_observer_skip(
    *,
    experience_id: str,
    position_id: str,
    reviewer_id: str,
    cycle_id: str,
    reason_code: str,
    reason: str,
    last_result: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Write a distinct append-only NOT_CALLED event for one reviewer in one manager cycle."""
    if not experience_id or not position_id or reviewer_id not in {"nemotron", "second_ai"}:
        raise ValueError("Valid experience, position and reviewer identifiers are required.")
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
        cycle_id=experience_id,
        phase="POSITION_AI_REVIEW_SKIPPED",
        event_key=f"position-review-skip:{position_id}:{cycle_id}:{reviewer_id}",
        details=details,
        experience_id=experience_id,
        position_id=position_id,
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
        "request_attempted": result.get("request_attempted") is True,
        "provider": str(result.get("provider") or ""),
        "model": str(result.get("model") or ""),
        "returned_model": str(result.get("returned_model") or ""),
        "request_started_at_utc": str(result.get("request_started_at_utc") or result.get("requested_at_utc") or ""),
        "response_completed_at_utc": str(result.get("response_completed_at_utc") or result.get("completed_at_utc") or ""),
        "latency_ms": result.get("latency_ms"),
        "http_status": result.get("http_status"),
        "response_validated": result.get("response_validated") is True,
        "model_verified": result.get("model_verified") is True,
        "verdict": str(result.get("verdict") or ""),
        "reason_fa": str(result.get("reason_fa") or result.get("reason") or "")[:1000],
        "observations": list(result.get("observations") or [])[:10],
        "risk_flags": list(result.get("risk_flags") or [])[:10],
        "skip_reason_code": str(result.get("skip_reason_code") or result.get("error_code") or ""),
        "configuration_issues": list(result.get("configuration_issues") or [])[:10],
        "execution_effect": "NONE",
        "observation_only": True,
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
    return {"event_id": saved.get("event_id"), "notion_sync_status": notion_status, **details}


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
        _record_audit_event(
            cycle_id=experience_id,
            phase="POSITION_AI_REVIEW_BATCH_COMPLETE",
            event_key=f"position-review-batch-complete:{position_id}:{review_batch_id}",
            details={
                "review_batch_id": review_batch_id,
                "consensus": consensus,
                "reviewers_recorded": sorted(seen),
                "execution_effect": "NONE",
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
                        "request_attempted": False,
                        "skip_reason_code": type(exc).__name__,
                        "reason": "Observer background worker failed before a validated result was stored.",
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
) -> dict[str, Any]:
    """Start two independent reviewers on a durable per-position schedule, never as an exit gate."""
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
    latest_results: dict[str, dict[str, Any]] = {}
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

    if latest_request is not None and len(latest_results) < 2:
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

    if latest_request is not None and latest_request_time is not None and len(latest_results) == 2:
        completed_times = [
            _parse_utc(item.get("response_completed_at_utc"))
            for item in latest_results.values()
        ]
        completed_times = [item for item in completed_times if item is not None]
        anchor = max(completed_times).timestamp() if completed_times else latest_request_time.timestamp()
        if now_epoch - anchor < interval_seconds:
            output: dict[str, Any] = {}
            for reviewer_id in ("nemotron", "second_ai"):
                previous = latest_results.get(reviewer_id, {})
                output[reviewer_id] = {
                    "status": "NOT_CALLED",
                    "reason_code": "REVIEW_NOT_DUE",
                    "request_attempted": False,
                    "last_result_status": previous.get("status"),
                    "last_verdict": previous.get("verdict"),
                    "last_result_event_id": previous.get("event_id"),
                    "last_result_age_seconds": max(0, int(now_epoch - anchor)),
                }
            return output

    review_batch_id = f"{position_id}:{int(now_epoch * 1000)}"
    _record_audit_event(
        cycle_id=experience_id,
        phase="POSITION_AI_REVIEW_BATCH_REQUESTED",
        event_key=f"position-review-batch-requested:{position_id}:{review_batch_id}",
        details={
            "review_batch_id": review_batch_id,
            "reviewer_ids": ["nemotron", "second_ai"],
            "status": "REQUESTED",
            "request_attempted": True,
            "request_started_at_utc": current.isoformat(),
            "schedule_interval_seconds": interval_seconds,
            "robot_decision": str(cycle_context.get("robot_decision") or "UNKNOWN"),
            "execution_effect": "NONE",
            "observation_only": True,
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
