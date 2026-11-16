"""Challenge 1 forward-only Mentor-assisted holdout rounds.

This runner is read-only with respect to BloFin: it fetches Demo Spot candles via
GET only, records paired BOT_ONLY and BOT_WITH_AI_MENTOR classifications, and
never places an order. Candidate models stay unpromoted.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

from .blofin_demo_spot_readonly import BloFinDemoSpotReadOnly
from .challenge_01_ai_mentor import (
    load_candidate_artifact,
    predict_candidate,
    review_baseline,
    train_candidate_from_completed_rounds,
    detect_candlestick_patterns,
    build_pattern_context,
    MARKET_CONTEXT_VERSION,
    FEATURE_WINDOW_LENGTH,
)
from .challenge_01_runner import (
    BAR_MILLISECONDS,
    CHALLENGE_ID,
    ENGINE_ID,
    MAX_ROUNDS,
    MIN_SAMPLES_PER_ROUND,
    SYMBOL,
    TIMEFRAME,
    Challenge01DataError,
    _classification_metrics,
    _classify_bot,
    _parse_candle,
    _reference_label,
)
from .database import connect_database, storage_backend
from .learning_experience_memory import LearningExperienceMemory


class Challenge01AssistedRunner:
    """Run one later, non-overlapping 100-pair forward holdout block."""

    def __init__(
        self,
        *,
        client: Any = None,
        connection_factory: Callable[[], Any] = connect_database,
        backend_factory: Callable[[], str] = storage_backend,
        allow_non_durable_for_tests: bool = False,
    ):
        self.client = client or BloFinDemoSpotReadOnly()
        self.connection_factory = connection_factory
        self.backend_factory = backend_factory
        self.allow_non_durable_for_tests = bool(allow_non_durable_for_tests)

    @staticmethod
    def _decision_and_outcome(item: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        events = item.get("events") or []
        decision = next((e for e in events if e.get("event_type") == "DECISION_RECORDED"), None)
        outcome = next((e for e in events if e.get("event_type") == "OUTCOME_LABELLED"), None)
        if decision is None or outcome is None:
            raise Challenge01DataError("PRIOR_ROUND_DECISION_OR_OUTCOME_MISSING")
        return decision, outcome

    def _previous_round_seed(
        self, memory: LearningExperienceMemory, previous_round: int,
    ) -> tuple[list[dict[str, Any]], int, str | None, str | None]:
        experiences = memory.recent_experiences(limit=1200, challenge_id=CHALLENGE_ID)
        by_target: dict[int, dict[str, Any]] = {}
        modes_by_target: dict[int, set[str]] = {}
        feature_by_target: dict[int, str] = {}
        feature_windows_by_target: dict[int, list[dict[str, Any]]] = {}
        model_hashes: set[str] = set()
        for item in experiences:
            decision_event = next(
                (e for e in item.get("events", []) if e.get("event_type") == "DECISION_RECORDED"),
                None,
            )
            if decision_event is None:
                continue
            payload = decision_event.get("payload") or {}
            snapshot = payload.get("input_snapshot") or {}
            if snapshot.get("round_number") != previous_round:
                continue
            target_time = snapshot.get("target_candle_open_time_ms")
            if not isinstance(target_time, int):
                target_time = snapshot.get("candle_open_time_ms")
            feature_time = snapshot.get("feature_candle_open_time_ms")
            feature = snapshot.get("feature_candle")
            feature_window = snapshot.get("feature_window")
            if (
                not isinstance(target_time, int)
                or not isinstance(feature_time, int)
                or target_time - feature_time != BAR_MILLISECONDS
                or not isinstance(feature, dict)
                or feature.get("open_time_ms") != feature_time
                or feature.get("confirmed") is not True
                or not isinstance(feature_window, list)
                or len(feature_window) != FEATURE_WINDOW_LENGTH
                or any(not isinstance(candle, dict) or candle.get("confirmed") is not True for candle in feature_window)
                or feature_window[-1] != feature
                or any(
                    right["open_time_ms"] - left["open_time_ms"] != BAR_MILLISECONDS
                    for left, right in zip(feature_window, feature_window[1:])
                )
                or snapshot.get("feature_window_open_times_ms")
                != [candle["open_time_ms"] for candle in feature_window]
                or snapshot.get("detected_candlestick_patterns")
                != detect_candlestick_patterns(feature_window)
                or snapshot.get("pattern_context") != build_pattern_context(feature_window)
                or snapshot.get("data_quality") != "VALID_COMPLETED_FEATURE_AND_TARGET"
                or snapshot.get("target_candle_in_input") is not False
                or "target_candle" in snapshot
            ):
                raise Challenge01DataError("PRIOR_ROUND_CANDLE_PATTERN_WINDOW_INVALID")
            decision_event, outcome_event = self._decision_and_outcome(item)
            if item.get("verification", {}).get("status") != "PASS":
                raise Challenge01DataError("PRIOR_ROUND_MEMORY_INTEGRITY_FAILED")
            decision_payload = decision_event.get("payload") or {}
            decision_data = decision_payload.get("decision") or {}
            outcome_payload = outcome_event.get("payload") or {}
            evidence = outcome_payload.get("evidence") or {}
            target = evidence.get("target_candle")
            expected_feature_times = [candle["open_time_ms"] for candle in feature_window]
            actual_label = _reference_label(target) if isinstance(target, dict) else None
            predicted_label = decision_data.get("candle_classification")
            expected_correctness = (
                "CORRECT" if predicted_label == actual_label else "INCORRECT"
            ) if actual_label is not None else None
            if (
                evidence.get("comparison") != "NEXT_CANDLE_DIRECTION_FROM_NEXT_COMPLETED_BAR"
                or evidence.get("no_future_data_used_in_input") is not True
                or evidence.get("feature_candle_open_time_ms") != feature_time
                or evidence.get("feature_window_open_times_ms") != expected_feature_times
                or evidence.get("target_candle_open_time_ms") != target_time
                or not isinstance(target, dict)
                or target.get("open_time_ms") != target_time
                or target.get("confirmed") is not True
                or decision_data.get("prediction_horizon_bars") != 1
                or evidence.get("reference_classification") != actual_label
                or outcome_payload.get("final") is not True
                or outcome_payload.get("decision_correctness") != expected_correctness
                or outcome_payload.get("realized_result") != "NOT_EXECUTED"
                or outcome_payload.get("realized_net_pnl") is not None
            ):
                raise Challenge01DataError("PRIOR_ROUND_TARGET_EVIDENCE_MISMATCH")
            mode = item.get("decision_mode", "")
            if mode == "BOT_WITH_AI_MENTOR":
                model_hash = decision_data.get("model_artifact_sha256")
                snapshot_hash = snapshot.get("mentor_model_artifact_sha256")
                if not isinstance(model_hash, str) or model_hash != snapshot_hash:
                    raise Challenge01DataError("PRIOR_ROUND_MENTOR_MODEL_HASH_MISMATCH")
                model_hashes.add(model_hash)
            feature_canonical = json.dumps(feature_window, sort_keys=True, separators=(",", ":"))
            prior_feature = feature_by_target.setdefault(target_time, feature_canonical)
            if prior_feature != feature_canonical:
                raise Challenge01DataError("PAIRED_ROUND_FEATURE_INPUT_MISMATCH")
            feature_windows_by_target[target_time] = feature_window
            if target_time in by_target and by_target[target_time] != target:
                raise Challenge01DataError("PAIRED_ROUND_TARGET_EVIDENCE_MISMATCH")
            by_target[target_time] = target
            modes_by_target.setdefault(target_time, set()).add(mode)
        if len(model_hashes) > 1:
            raise Challenge01DataError("PRIOR_ROUND_USED_MULTIPLE_MENTOR_MODELS")

        if len(by_target) != MIN_SAMPLES_PER_ROUND:
            raise Challenge01DataError("PRIOR_ROUND_MUST_HAVE_100_DISTINCT_TARGETS")
        if previous_round >= 2 and any(
            not {"BOT_ONLY", "BOT_WITH_AI_MENTOR"}.issubset(modes_by_target.get(ts, set()))
            for ts in by_target
        ):
            raise Challenge01DataError("PRIOR_ROUND_PAIRED_DECISIONS_INCOMPLETE")

        ordered_times = sorted(by_target)
        last_target_time = ordered_times[-1]
        last_window_times = ordered_times[-FEATURE_WINDOW_LENGTH:]
        if len(last_window_times) != FEATURE_WINDOW_LENGTH or any(
            right - left != BAR_MILLISECONDS
            for left, right in zip(last_window_times, last_window_times[1:])
        ):
            raise Challenge01DataError("PRIOR_ROUND_MISSING_FEATURE_WINDOW_SEED")
        last_window = [by_target[timestamp] for timestamp in last_window_times]
        if any(
            next_time - current_time != BAR_MILLISECONDS
            for current_time, next_time in zip(sorted(by_target), sorted(by_target)[1:])
        ):
            raise Challenge01DataError("PRIOR_ROUND_TARGETS_NOT_CONTIGUOUS")

        candidate_anchor = None
        candidate_hash = None
        for item in experiences:
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
                    saved_hash = artifact.pop("artifact_sha256", None)
                    calculated_hash = hashlib.sha256(json.dumps(
                        artifact, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                    ).encode("utf-8")).hexdigest()
                except (TypeError, ValueError, KeyError):
                    continue
                if (
                    saved_hash == calculated_hash
                    and artifact.get("schema") == "CSR_CH01_CANDLE_PATTERN_CONTEXT_SOFTMAX_V3"
                    and artifact.get("trained_through_round") == previous_round
                    and artifact.get("market_context_version") == MARKET_CONTEXT_VERSION
                    and artifact.get("market_context_scope") == "AUDIT_AND_REGIME_STRATIFICATION_ONLY_NOT_MODEL_FEATURES"
                    and artifact.get("promotion_status") == "CANDIDATE_NOT_PROMOTED"
                ):
                    candidate_anchor = item.get("experience_id")
                    candidate_hash = saved_hash
                    break
            if candidate_anchor:
                break
        if not candidate_anchor or not candidate_hash:
            raise Challenge01DataError("LATEST_ROUND_MENTOR_CANDIDATE_NOT_FOUND")
        return last_window, last_target_time, candidate_anchor, candidate_hash

    def run_round(self, *, round_number: int, sample_count: int = 100) -> dict[str, Any]:
        if isinstance(round_number, bool) or not isinstance(round_number, int) or not 2 <= round_number <= MAX_ROUNDS:
            raise ValueError("assisted round_number must be between 2 and 5")
        if isinstance(sample_count, bool) or not isinstance(sample_count, int) or sample_count != MIN_SAMPLES_PER_ROUND:
            raise ValueError("Challenge 1 assisted rounds require exactly 100 samples")

        backend = self.backend_factory()
        if backend != "TURSO_CLOUD_SQLITE" and not self.allow_non_durable_for_tests:
            return {
                "status": "BLOCKED",
                "reason_code": "DURABLE_LEARNING_MEMORY_REQUIRED",
                "memory_backend": backend,
                "orders_submitted": False,
            }

        connection = self.connection_factory()
        baseline_ids: list[str] = []
        assisted_ids: list[str] = []
        bot_predictions: list[str] = []
        mentor_predictions: list[str] = []
        references: list[str] = []
        inserted_count = 0
        try:
            memory = LearningExperienceMemory(connection)
            previous_seed, previous_target_time, anchor_id, expected_hash = self._previous_round_seed(
                memory, round_number - 1,
            )
            artifact, model_anchor = load_candidate_artifact(
                memory,
                anchor_experience_id=anchor_id,
                expected_sha256=expected_hash,
            )
            before_time = previous_target_time
            rows = self.client.get_candles(
                SYMBOL, bar=TIMEFRAME, limit=1440,
                before=before_time,
            )
            if not isinstance(rows, list) or not rows:
                raise Challenge01DataError("EMPTY_FORWARD_HOLDOUT_RESPONSE")
            parsed = [_parse_candle(row) for row in rows]
            times = [row["open_time_ms"] for row in parsed]
            if len(set(times)) != len(times):
                raise Challenge01DataError("DUPLICATE_FORWARD_HOLDOUT_TIMESTAMP")
            if any(times[index] <= times[index + 1] for index in range(len(times) - 1)):
                raise Challenge01DataError("FORWARD_HOLDOUT_ORDER_NOT_NEWEST_FIRST")
            if any(timestamp <= before_time for timestamp in times):
                raise Challenge01DataError("FORWARD_HOLDOUT_NOT_AFTER_PRIOR_ROUND")
            completed = [row for row in parsed if row["confirmed"]]
            if len(completed) < sample_count:
                raise Challenge01DataError("INSUFFICIENT_NEW_COMPLETED_HOLDOUT_CANDLES")
            targets = list(reversed(completed[-sample_count:]))
            for index, target in enumerate(targets):
                expected_time = before_time + (index + 1) * BAR_MILLISECONDS
                if target["open_time_ms"] != expected_time:
                    raise Challenge01DataError("FORWARD_HOLDOUT_HAS_GAP_OR_OVERLAP")

            feature_window = list(previous_seed)
            model_predictions_hash = hashlib.sha256()
            with connection:
                for target in targets:
                    feature = feature_window[-1]
                    feature_time = feature["open_time_ms"]
                    target_time = target["open_time_ms"]
                    if target_time - feature_time != BAR_MILLISECONDS:
                        raise Challenge01DataError("FEATURE_TARGET_NOT_ONE_BAR_APART")
                    cycle_id = f"CH01-R{round_number:02d}:{SYMBOL}:{TIMEFRAME}:{target_time}"
                    suffix = f"ch01-r{round_number:02d}-btc-usdt-1h-{target_time}"
                    baseline_id = suffix
                    assisted_id = suffix + "-mentor"
                    bot_prediction, bot_action = _classify_bot(feature)
                    mentor_prediction, probabilities = predict_candidate(feature_window, artifact)
                    actual = _reference_label(target)
                    bot_predictions.append(bot_prediction)
                    mentor_predictions.append(mentor_prediction)
                    references.append(actual)

                    base_decision = memory.record_decision(
                        experience_id=baseline_id,
                        event_id=f"{baseline_id}:decision",
                        engine_id=ENGINE_ID,
                        challenge_id=CHALLENGE_ID,
                        cycle_id=cycle_id,
                        decision_mode="BOT_ONLY",
                        environment="BLOFIN_DEMO",
                        market="SPOT",
                        symbol=SYMBOL,
                        timeframe=TIMEFRAME,
                        model_version="ch01-momentum-baseline-v2",
                        dataset_version=f"CH01-R{round_number:02d}-FORWARD-HOLDOUT",
                        decision={
                            "candle_classification": bot_prediction,
                            "action": bot_action,
                            "rule": "previous_candle_body_direction",
                            "prediction_horizon_bars": 1,
                            "confidence": None,
                            "confidence_scope": "UNSCORED_BASELINE_RULE_NOT_CALIBRATED_PROBABILITY",
                        },
                        input_snapshot={
                            "source": "blofin_demo_spot_public_candles",
                            "round_number": round_number,
                            "candle_open_time_ms": target_time,
                            "feature_candle_open_time_ms": feature_time,
                            "target_candle_open_time_ms": target_time,
                            "feature_candle": feature,
                            "feature_window": feature_window,
                            "feature_window_open_times_ms": [
                                candle["open_time_ms"] for candle in feature_window
                            ],
                            "detected_candlestick_patterns": detect_candlestick_patterns(feature_window),
                            "pattern_context": build_pattern_context(feature_window),
                            "prediction_horizon_bars": 1,
                            "data_quality": "VALID_COMPLETED_FEATURE_AND_TARGET",
                            "target_candle_in_input": False,
                        },
                    )
                    memory.record_outcome(
                        experience_id=baseline_id,
                        event_id=f"{baseline_id}:outcome",
                        decision_correctness="CORRECT" if bot_prediction == actual else "INCORRECT",
                        realized_result="NOT_EXECUTED",
                        realized_net_pnl=None,
                        final=True,
                        evidence={
                            "source": "blofin_demo_spot_next_completed_candle",
                            "comparison": "NEXT_CANDLE_DIRECTION_FROM_NEXT_COMPLETED_BAR",
                            "feature_candle_open_time_ms": feature_time,
                            "feature_window_open_times_ms": [
                                candle["open_time_ms"] for candle in feature_window
                            ],
                            "target_candle_open_time_ms": target_time,
                            "target_candle": target,
                            "reference_classification": actual,
                            "prediction_horizon_bars": 1,
                            "no_future_data_used_in_input": True,
                            "orders_submitted": False,
                        },
                    )
                    review_baseline(memory, baseline_id)
                    assisted_decision = memory.record_decision(
                        experience_id=assisted_id,
                        event_id=f"{assisted_id}:decision",
                        engine_id=ENGINE_ID,
                        challenge_id=CHALLENGE_ID,
                        cycle_id=cycle_id,
                        decision_mode="BOT_WITH_AI_MENTOR",
                        environment="BLOFIN_DEMO",
                        market="SPOT",
                        symbol=SYMBOL,
                        timeframe=TIMEFRAME,
                        baseline_experience_id=baseline_id,
                        model_version=artifact["model_version"],
                        dataset_version=f"CH01-R{round_number:02d}-FORWARD-HOLDOUT",
                        decision={
                            "candle_classification": mentor_prediction,
                            "action": "PREDICT_NEXT_" + mentor_prediction.removesuffix("_CANDLE"),
                            "probabilities": {
                                label: round(float(probability), 8)
                                for label, probability in zip(artifact["classes"], probabilities)
                            },
                            "rule": "MENTOR_SOFTMAX_CANDIDATE",
                            "prediction_horizon_bars": 1,
                            "confidence_scope": "MODEL_SCORE_NOT_CALIBRATED_PROBABILITY",
                            "model_artifact_sha256": artifact["artifact_sha256"],
                            "paired_bot_only_prediction": bot_prediction,
                        },
                        input_snapshot={
                            "source": "blofin_demo_spot_public_candles",
                            "round_number": round_number,
                            "candle_open_time_ms": target_time,
                            "feature_candle_open_time_ms": feature_time,
                            "target_candle_open_time_ms": target_time,
                            "feature_candle": feature,
                            "feature_window": feature_window,
                            "feature_window_open_times_ms": [
                                candle["open_time_ms"] for candle in feature_window
                            ],
                            "detected_candlestick_patterns": detect_candlestick_patterns(feature_window),
                            "pattern_context": build_pattern_context(feature_window),
                            "prediction_horizon_bars": 1,
                            "data_quality": "VALID_COMPLETED_FEATURE_AND_TARGET",
                            "target_candle_in_input": False,
                            "mentor_model_anchor_experience_id": model_anchor,
                            "mentor_model_artifact_sha256": artifact["artifact_sha256"],
                        },
                    )
                    memory.record_outcome(
                        experience_id=assisted_id,
                        event_id=f"{assisted_id}:outcome",
                        decision_correctness="CORRECT" if mentor_prediction == actual else "INCORRECT",
                        realized_result="NOT_EXECUTED",
                        realized_net_pnl=None,
                        final=True,
                        evidence={
                            "source": "blofin_demo_spot_next_completed_candle",
                            "comparison": "NEXT_CANDLE_DIRECTION_FROM_NEXT_COMPLETED_BAR",
                            "feature_candle_open_time_ms": feature_time,
                            "feature_window_open_times_ms": [
                                candle["open_time_ms"] for candle in feature_window
                            ],
                            "target_candle_open_time_ms": target_time,
                            "target_candle": target,
                            "reference_classification": actual,
                            "prediction_horizon_bars": 1,
                            "no_future_data_used_in_input": True,
                            "paired_bot_only_experience_id": baseline_id,
                            "model_artifact_sha256": artifact["artifact_sha256"],
                            "orders_submitted": False,
                        },
                    )
                    baseline_ids.append(baseline_id)
                    assisted_ids.append(assisted_id)
                    inserted_count += int(bool(base_decision.get("inserted")))
                    model_predictions_hash.update(f"{feature_time}:{mentor_prediction}:{target_time}:{actual}|".encode())
                    feature_window = feature_window[1:] + [target]

            dataset_hash = model_predictions_hash.hexdigest()
            connection.close()
            connection = self.connection_factory()
            recovered_memory = LearningExperienceMemory(connection)
            verified = 0
            for experience_id in baseline_ids + assisted_ids:
                recovered = recovered_memory.get_experience(experience_id)
                expected_status = "MENTOR_REVIEWED" if experience_id in baseline_ids else "OUTCOME_RECORDED"
                if recovered["verification"]["status"] != "PASS" or recovered["learning_status"] != expected_status:
                    raise RuntimeError("ASSISTED_ROUND_MEMORY_RECOVERY_FAILED")
                verified += 1

            next_model_candidate = None
            next_model_candidate_status = "NOT_REQUIRED_FINAL_ROUND"
            if round_number < MAX_ROUNDS:
                next_model_candidate = train_candidate_from_completed_rounds(
                    recovered_memory,
                    through_round=round_number,
                )
                next_model_candidate_status = "CANDIDATE_STORED_FOR_NEXT_ROUND"
            bot_metrics = _classification_metrics(bot_predictions, references)
            mentor_metrics = _classification_metrics(mentor_predictions, references)
            return {
                "status": "ROUND_RECORDED_NOT_MASTERY",
                "round_number": round_number,
                "sample_count": len(targets),
                "decision_mode": "BOT_WITH_AI_MENTOR_PAIRED_WITH_BOT_ONLY",
                "model_version": artifact["model_version"],
                "model_artifact_sha256": artifact["artifact_sha256"],
                "dataset_sha256": dataset_hash,
                "bot_only_metrics": bot_metrics,
                "mentor_metrics": mentor_metrics,
                "next_model_candidate": next_model_candidate,
                "next_model_candidate_status": next_model_candidate_status,
                "next_stage": (
                    "FORWARD_HOLDOUT_VALIDATION_REQUIRED"
                    if round_number < MAX_ROUNDS else "CHALLENGE_REVIEW_REQUIRED"
                ),
                "balanced_accuracy_delta": round(
                    mentor_metrics["balanced_accuracy"] - bot_metrics["balanced_accuracy"], 6,
                ),
                "memory_backend": backend,
                "memory_records_verified": verified,
                "memory_integrity": "PASS",
                "round_status": "EVALUATED_NOT_MASTERY",
                "mastery_awarded": False,
                "orders_submitted": False,
                "live_trading": False,
            }
        except Challenge01DataError as exc:
            return {
                "status": "BLOCKED",
                "reason_code": str(exc),
                "orders_submitted": False,
                "mastery_awarded": False,
            }
        except Exception as exc:
            return {
                "status": "UNKNOWN",
                "reason_code": f"{type(exc).__name__}:{str(exc)[:160]}",
                "orders_submitted": False,
                "mastery_awarded": False,
            }
        finally:
            try:
                connection.close()
            except Exception:
                pass
