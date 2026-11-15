"""Challenge 1 AI Mentor: audit the baseline without rewriting BOT_ONLY decisions.

The mentor is deliberately deterministic and provider-free for the zero-cost pilot.
It reviews the locked baseline's evidence, explains the task's limitations, and
records append-only feedback. It never changes a decision, submits orders, or
awards mastery.
"""
from __future__ import annotations

import hashlib
from typing import Any

from .learning_experience_memory import LearningExperienceMemory

MENTOR_VERSION = "ch01-audit-mentor-v1"
CHALLENGE_ID = "CHALLENGE-01"
EXPECTED_SCOPE = "CLOSED_CANDLE_OHLC_CLASSIFICATION_ONLY_NOT_FORWARD_PREDICTION_OR_PNL"
EXPECTED_COMPARISON = "DETERMINISTIC_OHLC_LABEL_CONSISTENCY_NOT_PREDICTIVE_GROUND_TRUTH"


def review_baseline(memory: LearningExperienceMemory, experience_id: str) -> dict[str, Any]:
    """Review one finalized BOT_ONLY experience and append evidence-bound feedback."""
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

    feedback = (
        "Baseline audit: " + (" ".join(issues) if issues else
        "Data and label integrity checks passed. However, Open-versus-Close "
        "classification uses the completed candle's final close to classify "
        "that same candle. It is a deterministic description, not a forecast "
        "of a future candle, profitable trade, or market edge. A 100% score "
        "is therefore not evidence of predictive learning. The next challenge "
        "dataset must define a future, non-overlapping target before any "
        "assisted round can count toward mastery.")
    proposed_change = (
        "Keep BOT_ONLY immutable. For future predictive evaluation, define the "
        "target on a later candle, freeze the feature timestamp before that "
        "target begins, and use non-overlapping holdout data. Until then return "
        "NOT_MASTERY."
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


__all__ = ["review_baseline", "MENTOR_VERSION"]
