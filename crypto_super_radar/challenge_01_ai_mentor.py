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



MODEL_CLASSES = ("BEARISH_CANDLE", "NEUTRAL_CANDLE", "BULLISH_CANDLE")
MODEL_FEATURES = ("body_return", "range_return", "close_location", "upper_wick_return", "lower_wick_return")
BAR_MILLISECONDS = 60 * 60 * 1000


def _target_label(candle: dict[str, Any]) -> str:
    opening = float(candle["open"])
    closing = float(candle["close"])
    return "BULLISH_CANDLE" if closing > opening else "BEARISH_CANDLE" if closing < opening else "NEUTRAL_CANDLE"


def _feature_vector(candle: dict[str, Any]) -> list[float]:
    opening, high, low, closing = (float(candle[key]) for key in ("open", "high", "low", "close"))
    if opening <= 0 or high < max(opening, closing, low) or low > min(opening, closing):
        raise ValueError("INVALID_FEATURE_CANDLE_OHLC")
    span = high - low
    return [
        (closing - opening) / opening,
        span / opening,
        (closing - low) / span if span else 0.5,
        (high - max(opening, closing)) / opening,
        (min(opening, closing) - low) / opening,
    ]


def _softmax(logits: list[float]) -> list[float]:
    maximum = max(logits)
    exps = [math.exp(max(-60.0, value - maximum)) for value in logits]
    total = sum(exps)
    return [value / total for value in exps]


def _train_candidate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Pure-Python multinomial logistic regression, chronological 80/20 split."""
    training_count = int(len(rows) * 0.8)
    train_rows, validation_rows = rows[:training_count], rows[training_count:]
    if len(train_rows) < 60 or len(validation_rows) < 20:
        raise ValueError("MENTOR_REQUIRES_80_TRAIN_20_TEMPORAL_VALIDATION")
    raw = [_feature_vector(row["feature"]) for row in train_rows]
    means = [sum(item[j] for item in raw) / len(raw) for j in range(len(MODEL_FEATURES))]
    stds = []
    for j, mean in enumerate(means):
        variance = sum((item[j] - mean) ** 2 for item in raw) / len(raw)
        stds.append(math.sqrt(variance) if variance > 1e-12 else 1.0)
    x_train = [[(item[j] - means[j]) / stds[j] for j in range(len(MODEL_FEATURES))] for item in raw]
    class_ids = {label: i for i, label in enumerate(MODEL_CLASSES)}
    y_train = [class_ids[row["label"]] for row in train_rows]
    weights = [[0.0] * len(MODEL_FEATURES) for _ in MODEL_CLASSES]
    biases = [0.0] * len(MODEL_CLASSES)
    # Fixed order and fixed hyperparameters make the same dataset reproducible.
    for _ in range(500):
        grad_w = [[0.0] * len(MODEL_FEATURES) for _ in MODEL_CLASSES]
        grad_b = [0.0] * len(MODEL_CLASSES)
        for features, expected in zip(x_train, y_train):
            probs = _softmax([
                biases[k] + sum(weights[k][j] * features[j] for j in range(len(MODEL_FEATURES)))
                for k in range(len(MODEL_CLASSES))
            ])
            for k, probability in enumerate(probs):
                error = probability - (1.0 if k == expected else 0.0)
                grad_b[k] += error
                for j, value in enumerate(features):
                    grad_w[k][j] += error * value
        rate = 0.08 / len(x_train)
        for k in range(len(MODEL_CLASSES)):
            biases[k] -= rate * grad_b[k]
            for j in range(len(MODEL_FEATURES)):
                weights[k][j] -= rate * (grad_w[k][j] + 0.001 * weights[k][j])

    def predict(candle: dict[str, Any]) -> tuple[str, list[float]]:
        values = _feature_vector(candle)
        normalized = [(values[j] - means[j]) / stds[j] for j in range(len(MODEL_FEATURES))]
        probabilities = _softmax([
            biases[k] + sum(weights[k][j] * normalized[j] for j in range(len(MODEL_FEATURES)))
            for k in range(len(MODEL_CLASSES))
        ])
        best = max(range(len(probabilities)), key=probabilities.__getitem__)
        return MODEL_CLASSES[best], probabilities

    predicted = [predict(row["feature"])[0] for row in validation_rows]
    actual = [row["label"] for row in validation_rows]
    per_class = {}
    recalls, f1s = [], []
    for label in MODEL_CLASSES:
        tp = sum(p == label and a == label for p, a in zip(predicted, actual))
        support = sum(a == label for a in actual)
        predicted_count = sum(p == label for p in predicted)
        precision = tp / predicted_count if predicted_count else 0.0
        recall = tp / support if support else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        if support:
            recalls.append(recall)
            f1s.append(f1)
        per_class[label] = {"support": support, "predicted": predicted_count, "precision": precision,
                            "recall": recall if support else None, "f1": f1}
    validation = {
        "sample_count": len(validation_rows),
        "accuracy": sum(p == a for p, a in zip(predicted, actual)) / len(actual),
        "balanced_accuracy": sum(recalls) / len(recalls) if recalls else 0.0,
        "macro_f1_present_classes": sum(f1s) / len(f1s) if f1s else 0.0,
        "per_class": per_class,
    }
    train_manifest = [[r["experience_id"], r["feature_ts"], r["target_ts"], r["label"]] for r in train_rows]
    validation_manifest = [[r["experience_id"], r["feature_ts"], r["target_ts"], r["label"]] for r in validation_rows]
    digest = lambda value: hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    artifact = {
        "schema": "CSR_CH01_SOFTMAX_CANDIDATE_V1",
        "model_version": "ch01-softmax-multiclass-v1",
        "task": "NEXT_CANDLE_DIRECTION_ONE_BAR_AHEAD",
        "method": "MULTINOMIAL_LOGISTIC_REGRESSION_SOFTMAX",
        "features": list(MODEL_FEATURES),
        "classes": list(MODEL_CLASSES),
        "normalization": {"means": means, "standard_deviations": stds},
        "weights": weights,
        "biases": biases,
        "training": {"samples": len(train_rows), "dataset_sha256": digest(train_manifest),
                     "first_target_time_ms": train_rows[0]["target_ts"], "last_target_time_ms": train_rows[-1]["target_ts"]},
        "temporal_validation": {"samples": len(validation_rows), "dataset_sha256": digest(validation_manifest),
                                "first_target_time_ms": validation_rows[0]["target_ts"],
                                "last_target_time_ms": validation_rows[-1]["target_ts"], "metrics": validation},
        "promotion_status": "CANDIDATE_NOT_PROMOTED",
        "mastery_awarded": False,
        "orders_submitted": False,
    }
    canonical = json.dumps(artifact, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    artifact_hash = hashlib.sha256(canonical.encode()).hexdigest()
    artifact["artifact_sha256"] = artifact_hash
    return {"artifact": artifact, "sha256": artifact_hash}


def _train_and_store_candidate(memory: LearningExperienceMemory, baseline_ids: list[str]) -> dict[str, Any]:
    rows = []
    for experience_id in baseline_ids:
        item = memory.get_experience(experience_id)
        if item.get("verification", {}).get("status") != "PASS":
            raise ValueError("BASELINE_MEMORY_INTEGRITY_FAILED")
        if item.get("decision_mode") != "BOT_ONLY" or item.get("challenge_id") != CHALLENGE_ID:
            raise ValueError("BASELINE_MODE_OR_CHALLENGE_MISMATCH")
        events = item.get("events") or []
        decision_event = next((e for e in events if e.get("event_type") == "DECISION_RECORDED"), None)
        outcome_event = next((e for e in events if e.get("event_type") == "OUTCOME_LABELLED"), None)
        if not decision_event or not outcome_event:
            raise ValueError("BASELINE_OUTCOME_NOT_FINAL")
        snapshot = decision_event.get("payload", {}).get("input_snapshot") or {}
        decision = decision_event.get("payload", {}).get("decision") or {}
        evidence = outcome_event.get("payload", {}).get("evidence") or {}
        feature, target = snapshot.get("feature_candle"), evidence.get("target_candle")
        feature_ts, target_ts = snapshot.get("feature_candle_open_time_ms"), snapshot.get("target_candle_open_time_ms")
        if (
            snapshot.get("round_number") != 1
            or snapshot.get("data_quality") != "VALID_COMPLETED_FEATURE_AND_TARGET"
            or snapshot.get("target_candle_in_input") is not False
            or "target_candle" in snapshot
            or evidence.get("no_future_data_used_in_input") is not True
            or evidence.get("comparison") != EXPECTED_COMPARISON
            or decision.get("rule") != "previous_candle_body_direction"
            or decision.get("prediction_horizon_bars") != 1
            or not isinstance(feature, dict) or not isinstance(target, dict)
            or not isinstance(feature_ts, int) or not isinstance(target_ts, int)
            or target_ts - feature_ts != BAR_MILLISECONDS
            or feature.get("open_time_ms") != feature_ts
            or target.get("open_time_ms") != target_ts
            or feature.get("confirmed") is not True or target.get("confirmed") is not True
        ):
            raise ValueError("FORWARD_FEATURE_TARGET_CONTRACT_FAILED")
        label = _target_label(target)
        rows.append({"experience_id": experience_id, "feature_ts": feature_ts,
                     "target_ts": target_ts, "feature": feature, "label": label})
    rows.sort(key=lambda row: row["feature_ts"])
    if len(rows) != 100:
        raise ValueError("MENTOR_REQUIRES_EXACTLY_100_FORWARD_SAMPLES")
    if len({row["target_ts"] for row in rows}) != len(rows):
        raise ValueError("DUPLICATE_TARGET_TIMESTAMP")
    for previous, current in zip(rows, rows[1:]):
        if current["feature_ts"] != previous["target_ts"]:
            raise ValueError("BASELINE_FEATURE_TARGET_SEQUENCE_GAP")

    trained = _train_candidate(rows)
    artifact, artifact_hash = trained["artifact"], trained["sha256"]
    anchor_id = rows[0]["experience_id"]
    anchor = memory.get_experience(anchor_id)
    existing = next((event for event in anchor["events"]
                     if event.get("event_type") == "LESSON_PROPOSED"
                     and (event.get("payload") or {}).get("lesson_id") == "ch01-model-" + artifact_hash[:24]), None)
    if not existing:
        evidence_ids = [event["event_id"] for event in anchor["events"]
                        if event.get("event_type") in {"DECISION_RECORDED", "OUTCOME_LABELLED", "MENTOR_FEEDBACK_RECORDED"}]
        with memory._db:
            memory.propose_lesson(
                experience_id=anchor_id,
                event_id="ch01-model-artifact-" + artifact_hash[:24],
                lesson_id="ch01-model-" + artifact_hash[:24],
                lesson=json.dumps(artifact, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
                evidence_event_ids=evidence_ids,
            )
    persisted = memory.get_experience(anchor_id)
    if persisted.get("verification", {}).get("status") != "PASS":
        raise ValueError("MENTOR_MODEL_ARTIFACT_INTEGRITY_FAILED")
    stored = next((event for event in persisted["events"]
                   if event.get("event_type") == "LESSON_PROPOSED"
                   and (event.get("payload") or {}).get("lesson_id") == "ch01-model-" + artifact_hash[:24]), None)
    if not stored:
        raise ValueError("MENTOR_MODEL_ARTIFACT_NOT_PERSISTED")
    saved_artifact = json.loads(stored["payload"]["lesson"])
    saved_hash = saved_artifact.pop("artifact_sha256", None)
    calculated_hash = hashlib.sha256(json.dumps(saved_artifact, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    if saved_hash != artifact_hash or calculated_hash != artifact_hash:
        raise ValueError("PERSISTED_MENTOR_MODEL_HASH_MISMATCH")
    return {
        "status": "CANDIDATE_STORED_NOT_PROMOTED",
        "model_version": artifact["model_version"],
        "artifact_sha256": artifact_hash,
        "training_samples": artifact["training"]["samples"],
        "validation_samples": artifact["temporal_validation"]["samples"],
        "training_dataset_sha256": artifact["training"]["dataset_sha256"],
        "validation_dataset_sha256": artifact["temporal_validation"]["dataset_sha256"],
        "validation_metrics": artifact["temporal_validation"]["metrics"],
        "promotion_status": "CANDIDATE_NOT_PROMOTED",
        "anchor_experience_id": anchor_id,
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
            decision_payload = decision_event.get("payload") or {}
            snapshot = decision_payload.get("input_snapshot") or {}
            evidence_event = next((e for e in item.get("events", []) if e.get("event_type") == "OUTCOME_LABELLED"), None)
            evidence = (evidence_event or {}).get("payload", {}).get("evidence") or {}
            if (
                snapshot.get("round_number") == 1
                and snapshot.get("data_quality") == "VALID_COMPLETED_FEATURE_AND_TARGET"
                and snapshot.get("target_candle_in_input") is False
                and "target_candle" not in snapshot
                and evidence.get("comparison") == EXPECTED_COMPARISON
            ):
                baseline_ids.append(item.get("experience_id"))

        baseline_ids = sorted(set(value for value in baseline_ids if value))
        if len(baseline_ids) != MIN_BASELINE_SAMPLES:
            return {
                "status": "BLOCKED",
                "reason_code": "BOT_ONLY_BASELINE_MUST_BE_EXACTLY_100_VALID_FORWARD_SAMPLES",
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
        model_candidate = _train_and_store_candidate(memory, baseline_ids)
        return {
            "status": "MENTOR_REVIEW_RECORDED",
            "challenge_id": CHALLENGE_ID,
            "memory_backend": backend,
            "baseline_samples": len(baseline_ids),
            "mentor_reviews_recorded": verified,
            "issues_found": sum(bool(result.get("issues")) for result in results),
            "mentor_model_version": MENTOR_VERSION,
            "model_candidate": model_candidate,
            "next_stage": "FORWARD_HOLDOUT_VALIDATION_REQUIRED",
            "mastery_awarded": False,
            "orders_submitted": False,
            "live_trading": False,
        }
    except Exception as exc:
        return {
            "status": "UNKNOWN",
            "reason_code": f"{type(exc).__name__}:{str(exc)[:160]}",
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
