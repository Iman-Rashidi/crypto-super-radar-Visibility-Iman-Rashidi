"""Challenge 1 deterministic audit mentor.

This zero-cost first version audits locked BOT_ONLY experiences. It is not an
LLM or predictive model: it records transparent, evidence-bound feedback and
never edits the baseline decision, submits orders, or awards mastery.
"""
from __future__ import annotations

import hashlib
import json
import math
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
    feature_window = snapshot.get("feature_window")
    if (
        not isinstance(feature_window, list)
        or len(feature_window) != FEATURE_WINDOW_LENGTH
        or any(not isinstance(candle, dict) or candle.get("confirmed") is not True for candle in feature_window)
        or (
            isinstance(feature_window, list)
            and len(feature_window) == FEATURE_WINDOW_LENGTH
            and snapshot.get("detected_candlestick_patterns") != detect_candlestick_patterns(feature_window)
        )
    ):
        issues.append("The three-candle feature window or its pattern evidence is missing or inconsistent.")
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
BAR_MILLISECONDS = 60 * 60 * 1000
FEATURE_WINDOW_LENGTH = 3
CANDLE_CONTINUOUS_FEATURES = (
    "body_return", "range_return", "close_location",
    "upper_wick_return", "lower_wick_return",
)
PATTERN_FEATURES = (
    "doji_last",
    "hammer_last",
    "inverted_hammer_last",
    "shooting_star_last",
    "hanging_man_last",
    "bullish_marubozu_last",
    "bearish_marubozu_last",
    "spinning_top_last",
    "bullish_engulfing_2bar",
    "bearish_engulfing_2bar",
    "bullish_harami_2bar",
    "bearish_harami_2bar",
    "piercing_line_2bar",
    "dark_cloud_cover_2bar",
    "morning_star_3bar",
    "evening_star_3bar",
    "three_white_soldiers_3bar",
    "three_black_crows_3bar",
    "tweezer_bottom_2bar",
    "tweezer_top_2bar",
)
MODEL_FEATURES = tuple(
    f"c{position}_{feature}"
    for position in range(1, FEATURE_WINDOW_LENGTH + 1)
    for feature in CANDLE_CONTINUOUS_FEATURES
) + PATTERN_FEATURES


def _target_label(candle: dict[str, Any]) -> str:
    opening = float(candle["open"])
    closing = float(candle["close"])
    return "BULLISH_CANDLE" if closing > opening else "BEARISH_CANDLE" if closing < opening else "NEUTRAL_CANDLE"


def _candle_shape(candle: dict[str, Any]) -> dict[str, float]:
    opening, high, low, closing = (float(candle[key]) for key in ("open", "high", "low", "close"))
    if opening <= 0 or high < max(opening, closing, low) or low > min(opening, closing):
        raise ValueError("INVALID_FEATURE_CANDLE_OHLC")
    span = high - low
    body_high, body_low = max(opening, closing), min(opening, closing)
    body = abs(closing - opening)
    return {
        "open": opening,
        "high": high,
        "low": low,
        "close": closing,
        "body": body,
        "range": span,
        "body_ratio": body / span if span else 0.0,
        "upper_wick": high - body_high,
        "lower_wick": body_low - low,
        "direction": 1.0 if closing > opening else -1.0 if closing < opening else 0.0,
    }


def detect_candlestick_patterns(candles: list[dict[str, Any]]) -> list[str]:
    """Detect transparent heuristic 1-3 candle patterns on completed bars only.

    Thresholds are engineering heuristics, not universal textbook definitions.
    The detector never reads a target candle outside the supplied feature window.
    """
    if not isinstance(candles, list) or len(candles) != FEATURE_WINDOW_LENGTH:
        raise ValueError("PATTERN_DETECTOR_REQUIRES_THREE_CANDLES")
    shapes = [_candle_shape(candle) for candle in candles]
    a, b, c = shapes
    patterns: list[str] = []
    eps = 1e-12

    # Most recent candle: single-candle patterns.
    if c["range"] > 0 and c["body_ratio"] <= 0.10:
        patterns.append("DOJI")
    if (
        c["body"] > eps and c["lower_wick"] >= 2.0 * c["body"]
        and c["upper_wick"] <= 0.5 * c["body"]
        and min(c["open"], c["close"]) >= c["low"] + 0.55 * c["range"]
    ):
        patterns.append("HAMMER")
    if (
        c["body"] > eps and c["upper_wick"] >= 2.0 * c["body"]
        and c["lower_wick"] <= 0.5 * c["body"]
        and max(c["open"], c["close"]) <= c["low"] + 0.45 * c["range"]
    ):
        patterns.append("INVERTED_HAMMER")
        patterns.append("SHOOTING_STAR")
    if (
        c["body"] > eps and c["lower_wick"] <= 0.5 * c["body"]
        and c["upper_wick"] >= 2.0 * c["body"]
        and max(c["open"], c["close"]) <= c["low"] + 0.45 * c["range"]
    ):
        patterns.append("HANGING_MAN")
    if c["range"] > 0 and c["body_ratio"] >= 0.80:
        patterns.append("BULLISH_MARUBOZU" if c["direction"] > 0 else "BEARISH_MARUBOZU")
    if (
        c["range"] > 0 and c["body_ratio"] <= 0.30
        and c["upper_wick"] >= 0.5 * max(c["body"], eps)
        and c["lower_wick"] >= 0.5 * max(c["body"], eps)
    ):
        patterns.append("SPINNING_TOP")

    # Two-candle patterns on the last two completed candles.
    a_low, a_high = min(a["open"], a["close"]), max(a["open"], a["close"])
    b_low, b_high = min(b["open"], b["close"]), max(b["open"], b["close"])
    if a["direction"] < 0 < b["direction"] and b_low <= a_low and b_high >= a_high:
        patterns.append("BULLISH_ENGULFING")
    if a["direction"] > 0 > b["direction"] and b_low <= a_low and b_high >= a_high:
        patterns.append("BEARISH_ENGULFING")
    if a["direction"] < 0 and b["direction"] > 0 and b_low >= a_low and b_high <= a_high:
        patterns.append("BULLISH_HARAMI")
    if a["direction"] > 0 and b["direction"] < 0 and b_low >= a_low and b_high <= a_high:
        patterns.append("BEARISH_HARAMI")
    a_mid = (a_low + a_high) / 2.0
    if a["direction"] < 0 < b["direction"] and b["open"] <= a["close"] and b["close"] > a_mid and b["close"] < a["open"]:
        patterns.append("PIERCING_LINE")
    if a["direction"] > 0 > b["direction"] and b["open"] >= a["close"] and b["close"] < a_mid and b["close"] > a["open"]:
        patterns.append("DARK_CLOUD_COVER")
    tweezer_tolerance = max((a["high"] + b["high"] + a["low"] + b["low"]) / 4.0 * 0.001, eps)
    if a["direction"] < 0 < b["direction"] and abs(a["low"] - b["low"]) <= tweezer_tolerance:
        patterns.append("TWEEZER_BOTTOM")
    if a["direction"] > 0 > b["direction"] and abs(a["high"] - b["high"]) <= tweezer_tolerance:
        patterns.append("TWEEZER_TOP")

    # Three-candle reversals/continuations. Crypto is a 24/7 market, so these
    # heuristic definitions do not require overnight price gaps.
    middle_small = b["range"] > 0 and b["body_ratio"] <= 0.30
    first_strong = a["range"] > 0 and a["body_ratio"] >= 0.50
    last_strong = c["range"] > 0 and c["body_ratio"] >= 0.40
    if a["direction"] < 0 and middle_small and c["direction"] > 0 and last_strong and c["close"] > a_mid:
        patterns.append("MORNING_STAR")
    if a["direction"] > 0 and middle_small and c["direction"] < 0 and last_strong and c["close"] < a_mid:
        patterns.append("EVENING_STAR")
    if (
        all(shape["direction"] > 0 for shape in shapes)
        and a["close"] < b["close"] < c["close"]
        and a["open"] <= b["open"] <= a["close"]
        and b["open"] <= c["open"] <= b["close"]
    ):
        patterns.append("THREE_WHITE_SOLDIERS")
    if (
        all(shape["direction"] < 0 for shape in shapes)
        and a["close"] > b["close"] > c["close"]
        and a["close"] <= b["open"] <= a["open"]
        and b["close"] <= c["open"] <= b["open"]
    ):
        patterns.append("THREE_BLACK_CROWS")
    return patterns


def _feature_vector(candles: list[dict[str, Any]]) -> list[float]:
    """Encode three completed candles plus explicit pattern flags, no target data."""
    if not isinstance(candles, list) or len(candles) != FEATURE_WINDOW_LENGTH:
        raise ValueError("FEATURE_WINDOW_REQUIRES_THREE_COMPLETED_CANDLES")
    values: list[float] = []
    for candle in candles:
        shape = _candle_shape(candle)
        opening = shape["open"]
        span = shape["range"]
        values.extend([
            (shape["close"] - opening) / opening,
            span / opening,
            (shape["close"] - shape["low"]) / span if span else 0.5,
            shape["upper_wick"] / opening,
            shape["lower_wick"] / opening,
        ])
    detected = set(detect_candlestick_patterns(candles))
    values.extend(1.0 if pattern in detected else 0.0 for pattern in PATTERN_FEATURES)
    return values


def _softmax(logits: list[float]) -> list[float]:
    maximum = max(logits)
    exps = [math.exp(max(-60.0, value - maximum)) for value in logits]
    total = sum(exps)
    return [value / total for value in exps]


def _train_candidate(
    rows: list[dict[str, Any]], *, trained_through_round: int,
) -> dict[str, Any]:
    """Pure-Python multinomial logistic regression, chronological 80/20 split."""
    training_count = int(len(rows) * 0.8)
    train_rows, validation_rows = rows[:training_count], rows[training_count:]
    if len(train_rows) < 60 or len(validation_rows) < 20:
        raise ValueError("MENTOR_REQUIRES_80_TRAIN_20_TEMPORAL_VALIDATION")
    raw = [_feature_vector(row["feature_window"]) for row in train_rows]
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

    def predict(candles: list[dict[str, Any]]) -> tuple[str, list[float]]:
        values = _feature_vector(candles)
        normalized = [(values[j] - means[j]) / stds[j] for j in range(len(MODEL_FEATURES))]
        probabilities = _softmax([
            biases[k] + sum(weights[k][j] * normalized[j] for j in range(len(MODEL_FEATURES)))
            for k in range(len(MODEL_CLASSES))
        ])
        best = max(range(len(probabilities)), key=probabilities.__getitem__)
        return MODEL_CLASSES[best], probabilities

    predicted = [predict(row["feature_window"])[0] for row in validation_rows]
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
    train_manifest = [
        [r["experience_id"], r["feature_ts"], r["target_ts"],
         [[candle["open_time_ms"], candle["open"], candle["high"], candle["low"], candle["close"]]
          for candle in r["feature_window"]], r["label"]]
        for r in train_rows
    ]
    validation_manifest = [
        [r["experience_id"], r["feature_ts"], r["target_ts"],
         [[candle["open_time_ms"], candle["open"], candle["high"], candle["low"], candle["close"]]
          for candle in r["feature_window"]], r["label"]]
        for r in validation_rows
    ]
    all_manifest = train_manifest + validation_manifest
    digest = lambda value: hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    dataset_hash = digest(all_manifest)
    artifact = {
        "schema": "CSR_CH01_CANDLE_PATTERN_SOFTMAX_V2",
        "feature_window_length": FEATURE_WINDOW_LENGTH,
        "pattern_features": list(PATTERN_FEATURES),
        "model_version": "ch01-candle-pattern-softmax-v2-" + dataset_hash[:12],
        "trained_through_round": trained_through_round,
        "task": "NEXT_CANDLE_DIRECTION_ONE_BAR_AHEAD",
        "method": "MULTINOMIAL_LOGISTIC_REGRESSION_WITH_CANDLE_PATTERNS",
        "features": list(MODEL_FEATURES),
        "pattern_features": list(PATTERN_FEATURES),
        "feature_window_length": FEATURE_WINDOW_LENGTH,
        "classes": list(MODEL_CLASSES),
        "normalization": {"means": means, "standard_deviations": stds},
        "weights": weights,
        "biases": biases,
        "training": {"samples": len(train_rows), "total_samples": len(rows), "dataset_sha256": digest(train_manifest),
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


def _train_and_store_candidate(
    memory: LearningExperienceMemory,
    baseline_ids: list[str],
    *,
    trained_through_round: int = 1,
    anchor_experience_id: str | None = None,
) -> dict[str, Any]:
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
        feedback_event = next((e for e in events if e.get("event_type") == "MENTOR_FEEDBACK_RECORDED"), None)
        if not decision_event or not outcome_event or not feedback_event:
            raise ValueError("BASELINE_OUTCOME_OR_MENTOR_REVIEW_NOT_FINAL")
        snapshot = decision_event.get("payload", {}).get("input_snapshot") or {}
        decision = decision_event.get("payload", {}).get("decision") or {}
        evidence = outcome_event.get("payload", {}).get("evidence") or {}
        round_number = snapshot.get("round_number")
        feature = snapshot.get("feature_candle")
        feature_window = snapshot.get("feature_window")
        target = evidence.get("target_candle")
        feature_ts, target_ts = snapshot.get("feature_candle_open_time_ms"), snapshot.get("target_candle_open_time_ms")
        if (
            not isinstance(round_number, int)
            or not 1 <= round_number <= trained_through_round
            or snapshot.get("data_quality") != "VALID_COMPLETED_FEATURE_AND_TARGET"
            or snapshot.get("target_candle_in_input") is not False
            or "target_candle" in snapshot
            or evidence.get("no_future_data_used_in_input") is not True
            or evidence.get("comparison") != EXPECTED_COMPARISON
            or decision.get("rule") != "previous_candle_body_direction"
            or decision.get("prediction_horizon_bars") != 1
            or not isinstance(feature, dict) or not isinstance(target, dict)
            or not isinstance(feature_window, list)
            or len(feature_window) != FEATURE_WINDOW_LENGTH
            or any(not isinstance(candle, dict) or candle.get("confirmed") is not True for candle in feature_window)
            or not isinstance(feature_ts, int) or not isinstance(target_ts, int)
            or target_ts - feature_ts != BAR_MILLISECONDS
            or feature.get("open_time_ms") != feature_ts
            or feature_window[-1] != feature
            or target.get("open_time_ms") != target_ts
            or feature.get("confirmed") is not True or target.get("confirmed") is not True
            or any(
                right["open_time_ms"] - left["open_time_ms"] != BAR_MILLISECONDS
                for left, right in zip(feature_window, feature_window[1:])
            )
            or snapshot.get("feature_window_open_times_ms") != [candle["open_time_ms"] for candle in feature_window]
            or snapshot.get("detected_candlestick_patterns") != detect_candlestick_patterns(feature_window)
            or evidence.get("feature_window_open_times_ms") != [candle["open_time_ms"] for candle in feature_window]
        ):
            raise ValueError("FORWARD_CANDLE_PATTERN_WINDOW_CONTRACT_FAILED")
        label = _target_label(target)
        rows.append({
            "experience_id": experience_id,
            "round_number": round_number,
            "feature_ts": feature_ts,
            "target_ts": target_ts,
            "feature": feature,
            "feature_window": feature_window,
            "label": label,
        })
    rows.sort(key=lambda row: row["feature_ts"])
    expected_total = trained_through_round * MIN_BASELINE_SAMPLES
    if len(rows) != expected_total:
        raise ValueError("MENTOR_REQUIRES_100_VALID_BOT_ONLY_SAMPLES_PER_COMPLETED_ROUND")
    if any(
        sum(1 for row in rows if row["round_number"] == number) != MIN_BASELINE_SAMPLES
        for number in range(1, trained_through_round + 1)
    ):
        raise ValueError("MENTOR_TRAINING_HISTORY_HAS_INCOMPLETE_ROUND")
    if len({row["target_ts"] for row in rows}) != len(rows):
        raise ValueError("DUPLICATE_TARGET_TIMESTAMP")
    for previous, current in zip(rows, rows[1:]):
        if current["feature_ts"] != previous["target_ts"]:
            raise ValueError("BASELINE_FEATURE_TARGET_SEQUENCE_GAP")

    trained = _train_candidate(rows, trained_through_round=trained_through_round)
    artifact, artifact_hash = trained["artifact"], trained["sha256"]
    anchor_id = anchor_experience_id or rows[0]["experience_id"]
    anchor = memory.get_experience(anchor_id)
    if anchor.get("decision_mode") != "BOT_ONLY" or anchor.get("verification", {}).get("status") != "PASS":
        raise ValueError("MENTOR_MODEL_ANCHOR_NOT_VERIFIED_BOT_ONLY")
    existing = next((
        event for event in anchor["events"]
        if event.get("event_type") == "LESSON_PROPOSED"
        and (event.get("payload") or {}).get("lesson_id") == "ch01-model-" + artifact_hash[:24]
    ), None)
    if not existing:
        evidence_ids = [
            event["event_id"] for event in anchor["events"]
            if event.get("event_type") in {
                "DECISION_RECORDED", "OUTCOME_LABELLED", "MENTOR_FEEDBACK_RECORDED",
            }
        ]
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
    stored = next((
        event for event in persisted["events"]
        if event.get("event_type") == "LESSON_PROPOSED"
        and (event.get("payload") or {}).get("lesson_id") == "ch01-model-" + artifact_hash[:24]
    ), None)
    if not stored:
        raise ValueError("MENTOR_MODEL_ARTIFACT_NOT_PERSISTED")
    saved_artifact = json.loads(stored["payload"]["lesson"])
    saved_hash = saved_artifact.pop("artifact_sha256", None)
    calculated_hash = hashlib.sha256(json.dumps(
        saved_artifact, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()).hexdigest()
    if saved_hash != artifact_hash or calculated_hash != artifact_hash:
        raise ValueError("PERSISTED_MENTOR_MODEL_HASH_MISMATCH")
    return {
        "status": "CANDIDATE_STORED_NOT_PROMOTED",
        "model_version": artifact["model_version"],
        "artifact_sha256": artifact_hash,
        "trained_through_round": trained_through_round,
        "training_samples": artifact["training"]["samples"],
        "validation_samples": artifact["temporal_validation"]["samples"],
        "total_samples": artifact["training"]["total_samples"],
        "training_dataset_sha256": artifact["training"]["dataset_sha256"],
        "validation_dataset_sha256": artifact["temporal_validation"]["dataset_sha256"],
        "validation_metrics": artifact["temporal_validation"]["metrics"],
        "promotion_status": "CANDIDATE_NOT_PROMOTED",
        "anchor_experience_id": anchor_id,
    }


def train_candidate_from_completed_rounds(
    memory: LearningExperienceMemory,
    *,
    through_round: int,
) -> dict[str, Any]:
    """Retrain only from completed, mentor-reviewed BOT_ONLY rounds before the next holdout."""
    if isinstance(through_round, bool) or not isinstance(through_round, int) or not 1 <= through_round <= 4:
        raise ValueError("through_round must be between 1 and 4")
    experiences = memory.recent_experiences(limit=1200, challenge_id=CHALLENGE_ID)
    ids_by_round: dict[int, list[tuple[int, str]]] = {}
    for item in experiences:
        if item.get("decision_mode") != "BOT_ONLY" or item.get("verification", {}).get("status") != "PASS":
            continue
        events = item.get("events") or []
        decision = next((e for e in events if e.get("event_type") == "DECISION_RECORDED"), None)
        if decision is None:
            continue
        snapshot = (decision.get("payload") or {}).get("input_snapshot") or {}
        number = snapshot.get("round_number")
        target_time = snapshot.get("target_candle_open_time_ms")
        if not isinstance(number, int) or not 1 <= number <= through_round or not isinstance(target_time, int):
            continue
        if not any(event.get("event_type") == "OUTCOME_LABELLED" for event in events):
            raise ValueError("TRAINING_ROUND_OUTCOME_NOT_FINAL")
        if not any(event.get("event_type") == "MENTOR_FEEDBACK_RECORDED" for event in events):
            raise ValueError("TRAINING_ROUND_MENTOR_REVIEW_MISSING")
        ids_by_round.setdefault(number, []).append((target_time, item["experience_id"]))

    ordered_ids: list[str] = []
    latest_round_anchor = None
    for number in range(1, through_round + 1):
        rows = sorted(ids_by_round.get(number, []))
        if len(rows) != MIN_BASELINE_SAMPLES or len({timestamp for timestamp, _ in rows}) != MIN_BASELINE_SAMPLES:
            raise ValueError("TRAINING_REQUIRES_100_REVIEWED_BOT_ONLY_SAMPLES_PER_ROUND")
        ordered_ids.extend(experience_id for _, experience_id in rows)
        if number == through_round:
            latest_round_anchor = rows[0][1]
    if latest_round_anchor is None:
        raise ValueError("TRAINING_ANCHOR_NOT_FOUND")
    return _train_and_store_candidate(
        memory,
        ordered_ids,
        trained_through_round=through_round,
        anchor_experience_id=latest_round_anchor,
    )



def load_candidate_artifact(
    memory: LearningExperienceMemory,
    *,
    anchor_experience_id: str | None = None,
    expected_sha256: str | None = None,
) -> tuple[dict[str, Any], str]:
    """Load and re-hash a persisted, unpromoted candidate model."""
    candidates = (
        [memory.get_experience(anchor_experience_id)]
        if anchor_experience_id
        else memory.recent_experiences(limit=1200, challenge_id=CHALLENGE_ID)
    )
    for item in candidates:
        if item.get("verification", {}).get("status") != "PASS":
            continue
        for event in item.get("events") or []:
            if event.get("event_type") != "LESSON_PROPOSED":
                continue
            payload = event.get("payload") or {}
            if not str(payload.get("lesson_id", "")).startswith("ch01-model-"):
                continue
            try:
                artifact = json.loads(payload.get("lesson", ""))
                saved_sha = artifact.pop("artifact_sha256", None)
                calculated = hashlib.sha256(json.dumps(
                    artifact, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                ).encode("utf-8")).hexdigest()
                if (
                    saved_sha != calculated
                    or (expected_sha256 is not None and saved_sha != expected_sha256)
                    or artifact.get("schema") != "CSR_CH01_CANDLE_PATTERN_SOFTMAX_V2"
                    or artifact.get("feature_window_length") != FEATURE_WINDOW_LENGTH
                    or artifact.get("features") != list(MODEL_FEATURES)
                    or artifact.get("pattern_features") != list(PATTERN_FEATURES)
                    or artifact.get("promotion_status") != "CANDIDATE_NOT_PROMOTED"
                    or artifact.get("training", {}).get("samples", 0) < 60
                    or artifact.get("temporal_validation", {}).get("samples", 0) < 20
                    or artifact.get("training", {}).get("total_samples")
                    != artifact.get("training", {}).get("samples", 0)
                    + artifact.get("temporal_validation", {}).get("samples", 0)
                    or not isinstance(artifact.get("trained_through_round"), int)
                ):
                    continue
                artifact["artifact_sha256"] = saved_sha
                return artifact, item["experience_id"]
            except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                continue
    raise ValueError("VERIFIED_MENTOR_CANDIDATE_ARTIFACT_NOT_FOUND")


def predict_candidate(
    feature_window: list[dict[str, Any]],
    artifact: dict[str, Any],
) -> tuple[str, list[float]]:
    """Apply the persisted candidate without refitting or seeing target data."""
    if (
        artifact.get("schema") != "CSR_CH01_CANDLE_PATTERN_SOFTMAX_V2"
        or artifact.get("promotion_status") != "CANDIDATE_NOT_PROMOTED"
        or artifact.get("features") != list(MODEL_FEATURES)
        or artifact.get("feature_window_length") != FEATURE_WINDOW_LENGTH
        or artifact.get("pattern_features") != list(PATTERN_FEATURES)
        or artifact.get("classes") != list(MODEL_CLASSES)
    ):
        raise ValueError("MENTOR_CANDIDATE_SCHEMA_OR_STATUS_INVALID")
    normalization = artifact.get("normalization") or {}
    means = normalization.get("means") or []
    stds = normalization.get("standard_deviations") or []
    weights = artifact.get("weights") or []
    biases = artifact.get("biases") or []
    if (
        len(means) != len(MODEL_FEATURES)
        or len(stds) != len(MODEL_FEATURES)
        or len(weights) != len(MODEL_CLASSES)
        or len(biases) != len(MODEL_CLASSES)
        or any(len(row) != len(MODEL_FEATURES) for row in weights)
        or any(not math.isfinite(float(value)) for value in means + stds + biases)
        or any(not math.isfinite(float(value)) for row in weights for value in row)
        or any(float(value) <= 0 for value in stds)
    ):
        raise ValueError("MENTOR_CANDIDATE_PARAMETERS_INVALID")
    raw = _feature_vector(feature_window)
    normalized = [(raw[j] - float(means[j])) / float(stds[j]) for j in range(len(MODEL_FEATURES))]
    probabilities = _softmax([
        float(biases[k]) + sum(
            float(weights[k][j]) * normalized[j] for j in range(len(MODEL_FEATURES))
        )
        for k in range(len(MODEL_CLASSES))
    ])
    best = max(range(len(probabilities)), key=probabilities.__getitem__)
    return MODEL_CLASSES[best], probabilities

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


__all__ = ["review_baseline", "review_locked_baseline", "load_candidate_artifact", "predict_candidate", "train_candidate_from_completed_rounds", "MENTOR_VERSION"]
