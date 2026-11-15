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
EXPECTED_SCOPE = "CLOSED_CANDLE_OHLC_CLASSIFICATION_ONLY_NOT_FORWARD_PREDICTION_OR_PNL"
EXPECTED_COMPARISON = "DETERMINISTIC_OHLC_LABEL_CONSISTENCY_NOT_PREDICTIVE_GROUND_TRUTH"
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
    if snapshot.get("data_quality") != "VALID_COMPLETED_CANDLE":
        issues.append("Data quality is not verified as a completed candle.")
    if evidence.get("comparison") != EXPECTED_COMPARISON:
        issues.append("The outcome label is not independently grounded.")
    if decision.get("confidence_scope") != (
        "DETERMINISTIC_CLOSED_CANDLE_CLASSIFICATION_NOT_MARKET_FORECAST"
    ):
        issues.append("Confidence scope is missing or overstated.")
    if outcome_payload.get("realized_result") != "NOT_EXECUTED":
        issues.append("This exercise must not be interpreted as a trade result.")

    finding_text = " ".join(issues) if issues else "Data and label integrity checks passed."
    feedback = (
        "Baseline audit: " + finding_text + " Open-versus-Close classification "
        "uses the completed candle's final close to classify that same candle. "
        "It is a deterministic description, not a forecast of a future candle, "
        "profitable trade, or market edge. A 100% score is not evidence of "
        "predictive learning. A future predictive task must define its target "
        "on a later candle, freeze feature time before that target begins, and "
        "use non-overlapping holdout data."
    )
    proposed_change = (
        "Keep BOT_ONLY immutable. Before any assisted round, define a later-candle "
        "target, freeze feature timestamps before the target period, and use "
        "non-overlapping holdout data. Until this is implemented and tested, "
        "return NOT_MASTERY."
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
