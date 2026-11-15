"""Challenge 1 deterministic audit mentor.

This zero-cost first version audits locked BOT_ONLY experiences. It is not an
LLM or predictive model: it records transparent, evidence-bound feedback and
never edits the baseline decision, submits orders, or awards mastery.
"""
from __future__ import annotations

import hashlib
from typing import Any, Callable

from .database import connect_database, storage_backend
from .learning_experience_memory import LearningExperienceMemory

MENTOR_VERSION = "ch01-audit-mentor-v1"
CHALLENGE_ID = "CHALLENGE-01"
EXPECTED_SCOPE = "NEXT_CANDLE_DIRECTION_ONE_BAR_AHEAD_HISTORICAL_REPLAY_NOT_PNL"
EXPECTED_COMPARISON = "NEXT_CANDLE_DIRECTION_FROM_NEXT_COMPLETED_BAR"
MIN_BASELINE_SAMPLES = 100


def review_baseline(memory: LearningExperienceMemory, experience_id: str) -> dict[str, Any]:
    """Audit one finalized BOT_ONLY experience and append immutable feedback."""
    item = memory.get_experience(experience_id)
    if item.get("challenge_id") != CHALLENGE_ID or item.get("decision_mode") != "BOT_ONLY":
        raise ValueError("MENTOR_REQUIRES_CHALLENGE_01_BOT_ONLY_BASELINE")
    events = item.get("events") or []
    decision_event = next((e for e in events if e.get("event_type") == "DECISION_RECORDED"), None)
    outcome_event = next((e for e in events if e.get("event_type") == "OUTCOME_LABELLED"), None)
    if not decision_event or not outcome_event:
        raise ValueError("MENTOR_REQUIRES_LOCKED_DECISION_AND_FINAL_OUTCOME")
    decision_payload = decision_event.get("payload") or {}
    outcome_payload = outcome_event.get("payload") or {}
    decision = decision_payload.get("decision") or {}
    evidence = outcome_payload.get("evidence") or {}
    snapshot = decision_payload.get("input_snapshot") or {}

    issues = []
    feature_ts = snapshot.get("feature_candle_open_time_ms")
    target_ts = snapshot.get("target_candle_open_time_ms")
    target_candle = evidence.get("target_candle")
    if snapshot.get("data_quality") != "VALID_COMPLETED_FEATURE_AND_TARGET":
        issues.append("The baseline is not marked as a valid completed feature/target pair.")
    if evidence.get("comparison") != EXPECTED_COMPARISON:
        issues.append("The outcome label is not a verified next-candle target.")
    if decision.get("rule") != "previous_candle_body_direction":
        issues.append("The baseline is not the declared one-bar momentum rule.")
    if decision.get("prediction_horizon_bars") != 1:
        issues.append("The prediction horizon is not exactly one completed bar.")
    if not isinstance(feature_ts, int) or not isinstance(target_ts, int) or target_ts <= feature_ts:
        issues.append("The target timestamp does not strictly follow the feature timestamp.")
    if "target_candle" in snapshot or snapshot.get("target_candle_in_input") is not False:
        issues.append("Future target data appears in, or is not explicitly excluded from, the decision input.")
    if evidence.get("no_future_data_used_in_input") is not True:
        issues.append("The outcome evidence does not certify feature/target isolation.")
    if not isinstance(target_candle, dict) or target_candle.get("open_time_ms") != target_ts:
        issues.append("The outcome does not contain the completed target candle matching its timestamp.")
    if outcome_payload.get("realized_result") != "NOT_EXECUTED":
        issues.append("This exercise must not be interpreted as a trade result.")

    finding_text = " ".join(issues) if issues else "Feature/target chronology and input isolation checks passed."
    feedback = (
        "Baseline audit: " + finding_text + " The current BOT_ONLY baseline is a "
        "naive one-bar momentum rule: it copies the prior completed candle's body "
        "direction to predict the next completed candle. This is a genuine "
        "one-step-ahead historical classification test, but it is not a trained "
        "AI model, not a calibrated probability, and not proof of a profitable "
        "market edge. Its score is not net trading performance, does not include "
        "fees or execution, and must be confirmed on later non-overlapping data. "
        "A successful baseline only establishes a benchmark for Mentor-assisted "
        "evaluation; it does not earn mastery."
    )
    proposed_change = (
        "Keep the BOT_ONLY baseline and outcomes immutable. Train a candidate "
        "model only from the frozen round-one feature/target pairs, record the "
        "model artifact hash and version, and compare it against BOT_ONLY on "
        "strictly later non-overlapping samples. Do not reuse round-one targets "
        "as a scored holdout and do not award mastery from baseline accuracy."
    )
    event_id = "ch01-mentor-" + hashlib.sha256(experience_id.encode("utf-8")).hexdigest()[:24]
    with memory._db:
        recorded = memory.record_mentor_feedback(
            experience_id=experience_id,
            event_id=event_id,
            mentor_model_version=MENTOR_VERSION,
            feedback=feedback,
            proposed_change=proposed_change,
            evidence={
                "source": "challenge_01_baseline_audit",
                "decision_event_id": decision_event.get("event_id"),
                "outcome_event_id": outcome_event.get("event_id"),
                "metric_scope": EXPECTED_SCOPE,
                "mastery_awarded": False,
                "orders_submitted": False,
            },
        )
    return {
        "status": "MENTOR_REVIEW_RECORDED",
        "challenge_id": CHALLENGE_ID,
        "experience_id": experience_id,
        "mentor_model_version": MENTOR_VERSION,
        "feedback_event_id": recorded.get("event_id"),
        "issues": issues,
        "mastery_awarded": False,
        "bot_only_decision_changed": False,
        "orders_submitted": False,
        "live_trading": False,
    }


def review_locked_baseline(
    *,
    connection_factory: Callable[[], Any] = connect_database,
    backend_factory: Callable[[], str] = storage_backend,
    allow_non_durable_for_tests: bool = False,
) -> dict[str, Any]:
    """Review all 100+ locked round-one experiences, requiring durable storage."""
    connection = None
    reviewed_count = 0
    try:
        backend = backend_factory()
        if backend != "TURSO_CLOUD_SQLITE" and not allow_non_durable_for_tests:
            return {
                "status": "BLOCKED",
                "reason_code": "DURABLE_LEARNING_MEMORY_REQUIRED",
                "memory_backend": backend,
                "mentor_reviews_recorded": 0,
                "mastery_awarded": False,
                "orders_submitted": False,
            }

        connection = connection_factory()
        memory = LearningExperienceMemory(connection)
        experiences = memory.recent_experiences(limit=500, challenge_id=CHALLENGE_ID)
        baseline_ids = []
        for item in experiences:
            if item.get("decision_mode") != "BOT_ONLY":
                continue
            decision_event = next(
                (e for e in item.get("events", []) if e.get("event_type") == "DECISION_RECORDED"),
                None,
            )
            if not decision_event:
                continue
            snapshot = (decision_event.get("payload") or {}).get("input_snapshot") or {}
            if snapshot.get("round_number") == 1:
                baseline_ids.append(item.get("experience_id"))

        baseline_ids = sorted(set(value for value in baseline_ids if value))
        if len(baseline_ids) < MIN_BASELINE_SAMPLES:
            return {
                "status": "BLOCKED",
                "reason_code": "BOT_ONLY_BASELINE_INCOMPLETE",
                "baseline_samples": len(baseline_ids),
                "required_samples": MIN_BASELINE_SAMPLES,
                "mentor_reviews_recorded": 0,
                "mastery_awarded": False,
                "orders_submitted": False,
            }

        # Preflight every experience before writing any feedback.
        for experience_id in baseline_ids:
            item = memory.get_experience(experience_id)
            if item.get("verification", {}).get("status") != "PASS":
                return {
                    "status": "BLOCKED",
                    "reason_code": "BASELINE_MEMORY_INTEGRITY_FAILED",
                    "experience_id": experience_id,
                    "mentor_reviews_recorded": 0,
                    "mastery_awarded": False,
                    "orders_submitted": False,
                }
            types = [event.get("event_type") for event in item.get("events", [])]
            if "OUTCOME_LABELLED" not in types:
                return {
                    "status": "BLOCKED",
                    "reason_code": "BASELINE_OUTCOME_NOT_FINAL",
                    "experience_id": experience_id,
                    "mentor_reviews_recorded": 0,
                    "mastery_awarded": False,
                    "orders_submitted": False,
                }

        results = []
        for experience_id in baseline_ids:
            results.append(review_baseline(memory, experience_id))
            reviewed_count += 1
        verified = sum(
            1 for experience_id in baseline_ids
            if any(
                event.get("event_type") == "MENTOR_FEEDBACK_RECORDED"
                for event in memory.get_experience(experience_id).get("events", [])
            )
        )
        if verified != len(baseline_ids):
            return {
                "status": "UNKNOWN",
                "reason_code": "MENTOR_FEEDBACK_VERIFICATION_INCOMPLETE",
                "baseline_samples": len(baseline_ids),
                "mentor_reviews_recorded": verified,
                "mastery_awarded": False,
                "orders_submitted": False,
            }
        return {
            "status": "MENTOR_REVIEW_RECORDED",
            "challenge_id": CHALLENGE_ID,
            "memory_backend": backend,
            "baseline_samples": len(baseline_ids),
            "mentor_reviews_recorded": verified,
            "issues_found": sum(bool(result.get("issues")) for result in results),
            "mentor_model_version": MENTOR_VERSION,
            "next_stage": "ASSISTED_ROUND_RUNNER_REQUIRED",
            "mastery_awarded": False,
            "orders_submitted": False,
            "live_trading": False,
        }
    except Exception as exc:
        return {
            "status": "UNKNOWN",
            "reason_code": type(exc).__name__,
            "mentor_reviews_recorded": reviewed_count,
            "partial_write_possible": reviewed_count > 0,
            "retry_safe": True,
            "mastery_awarded": False,
            "orders_submitted": False,
        }
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


__all__ = ["review_baseline", "review_locked_baseline", "MENTOR_VERSION"]
