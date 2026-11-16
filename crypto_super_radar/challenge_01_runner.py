"""Challenge 1: completed single-candle classification with append-only memory.

This module reads only the public BloFin Demo Spot candle endpoint. It has no
order, account, transfer, or withdrawal capability. The result is a candle
classification exercise, not a price forecast or trading-performance metric.
"""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from .blofin_demo_spot_readonly import BloFinDemoSpotReadOnly
from .database import connect_database, storage_backend
from .learning_experience_memory import LearningExperienceMemory
from .challenge_01_ai_mentor import CONTEXT_WINDOW_LENGTH, FEATURE_WINDOW_LENGTH, MARKET_CONTEXT_VERSION, build_pattern_context, detect_candlestick_patterns


CHALLENGE_ID = "CHALLENGE-01"
ENGINE_ID = "CH01_SINGLE_CANDLE"
SYMBOL = "BTC-USDT"
TIMEFRAME = "1H"
BAR_MILLISECONDS = 60 * 60 * 1000
MAX_STALE_AFTER_CLOSE_MS = 2 * BAR_MILLISECONDS + 5 * 60 * 1000
MIN_SAMPLES_PER_ROUND = 100
MAX_ROUNDS = 5
CLASS_LABELS = ("BULLISH_CANDLE", "BEARISH_CANDLE", "NEUTRAL_CANDLE")


class Challenge01DataError(ValueError):
    """Invalid, stale, incomplete, duplicated, or insufficient Challenge 1 data."""


def _decimal(value: Any, field: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise Challenge01DataError(f"INVALID_{field.upper()}") from exc
    if not number.is_finite():
        raise Challenge01DataError(f"NON_FINITE_{field.upper()}")
    return number


def _parse_candle(row: Any) -> dict[str, Any]:
    if isinstance(row, (list, tuple)):
        if len(row) < 9:
            raise Challenge01DataError("CANDLE_FIELD_MISSING")
        raw = {
            "ts": row[0], "open": row[1], "high": row[2], "low": row[3],
            "close": row[4], "volume": row[5], "confirm": row[8],
        }
    elif isinstance(row, dict):
        raw = row
    else:
        raise Challenge01DataError("INVALID_CANDLE_SHAPE")

    try:
        timestamp = int(str(raw["ts"]))
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise Challenge01DataError("INVALID_CANDLE_TIMESTAMP") from exc
    # BloFin timestamps are Unix milliseconds. Seconds or ambiguous units are rejected.
    if timestamp < 100_000_000_000:
        raise Challenge01DataError("TIMESTAMP_NOT_MILLISECONDS")

    prices = {
        name: _decimal(raw.get(name), name)
        for name in ("open", "high", "low", "close")
    }
    if any(value <= 0 for value in prices.values()):
        raise Challenge01DataError("NON_POSITIVE_OHLC")
    if prices["high"] < max(prices["open"], prices["close"], prices["low"]):
        raise Challenge01DataError("INCONSISTENT_HIGH")
    if prices["low"] > min(prices["open"], prices["close"], prices["high"]):
        raise Challenge01DataError("INCONSISTENT_LOW")
    if prices["high"] < prices["low"]:
        raise Challenge01DataError("INCONSISTENT_RANGE")

    volume = _decimal(raw.get("volume", raw.get("vol", 0)), "volume")
    if volume < 0:
        raise Challenge01DataError("NEGATIVE_CANDLE_VOLUME")
    confirm_raw = raw.get("confirm")
    if str(confirm_raw) not in {"0", "1"}:
        raise Challenge01DataError("INVALID_CANDLE_CONFIRM")
    return {
        "open_time_ms": timestamp,
        **{key: str(value) for key, value in prices.items()},
        "volume": str(volume),
        "confirmed": str(confirm_raw) == "1",
    }


def _classify_bot(feature_candle: dict[str, Any]) -> tuple[str, str]:
    """One-bar momentum baseline: prior candle body predicts the next candle class."""
    opening = Decimal(feature_candle["open"])
    closing = Decimal(feature_candle["close"])
    if closing > opening:
        return "BULLISH_CANDLE", "PREDICT_NEXT_BULLISH"
    if closing < opening:
        return "BEARISH_CANDLE", "PREDICT_NEXT_BEARISH"
    return "NEUTRAL_CANDLE", "PREDICT_NEXT_NEUTRAL"


def _classification_metrics(
    predictions: list[str], references: list[str],
) -> dict[str, Any]:
    """Compute descriptive classification metrics against the OHLC label rule."""
    if not predictions or len(predictions) != len(references):
        raise ValueError("predictions and references must have equal non-empty lengths")
    labels = tuple(sorted(set(predictions) | set(references)))
    confusion = {
        actual: {predicted: 0 for predicted in CLASS_LABELS}
        for actual in CLASS_LABELS
    }
    for predicted, actual in zip(predictions, references):
        if predicted not in CLASS_LABELS or actual not in CLASS_LABELS:
            raise ValueError("unknown Challenge 1 class label")
        confusion[actual][predicted] += 1

    recalls = []
    f1_scores = []
    per_class = {}
    for label in labels:
        true_positive = confusion[label][label]
        support = sum(confusion[label].values())
        predicted_count = sum(confusion[actual][label] for actual in CLASS_LABELS)
        recall = true_positive / support if support else 0.0
        precision = true_positive / predicted_count if predicted_count else 0.0
        f1 = (2 * precision * recall / (precision + recall)) if precision + recall else 0.0
        recalls.append(recall)
        f1_scores.append(f1)
        per_class[label] = {
            "support": support,
            "precision": round(precision, 6),
            "recall": round(recall, 6),
            "f1": round(f1, 6),
        }

    bullish_predicted = sum(confusion[actual]["BULLISH_CANDLE"] for actual in labels)
    bullish_true_positive = confusion.get("BULLISH_CANDLE", {}).get("BULLISH_CANDLE", 0)
    signal_precision = (
        bullish_true_positive / bullish_predicted if bullish_predicted else None
    )
    return {
        "balanced_accuracy": round(sum(recalls) / len(recalls), 6),
        "macro_f1": round(sum(f1_scores) / len(f1_scores), 6),
        "signal_precision": round(signal_precision, 6) if signal_precision is not None else None,
        "per_class": per_class,
        "confusion_matrix": confusion,
        "metric_scope": "NEXT_CANDLE_DIRECTION_ONE_BAR_AHEAD_HISTORICAL_REPLAY_NOT_PNL",
    }


def _reference_label(candle: dict[str, Any]) -> str:
    """Separate reference implementation for the deterministic OHLC rule."""
    opening = Decimal(candle["open"])
    closing = Decimal(candle["close"])
    relation = (closing > opening) - (closing < opening)
    return {1: "BULLISH_CANDLE", -1: "BEARISH_CANDLE", 0: "NEUTRAL_CANDLE"}[relation]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _round_prefix(round_number: int) -> str:
    return f"CH01-R{round_number:02d}"


def _existing_rounds(memory: LearningExperienceMemory) -> dict[int, list[int]]:
    found: dict[int, list[int]] = {}
    for item in memory.recent_experiences(limit=500, challenge_id=CHALLENGE_ID):
        events = item.get("events") or []
        if not events:
            continue
        decision = next(
            (event.get("payload", {}) for event in events if event.get("event_type") == "DECISION_RECORDED"),
            {},
        )
        snapshot = decision.get("input_snapshot") or {}
        round_number = snapshot.get("round_number")
        candle_time = snapshot.get("candle_open_time_ms")
        if isinstance(round_number, int) and isinstance(candle_time, int):
            found.setdefault(round_number, []).append(candle_time)
    return found


class Challenge01Runner:
    def __init__(
        self,
        *,
        client: Any = None,
        connection_factory: Callable[[], Any] = connect_database,
        backend_factory: Callable[[], str] = storage_backend,
        now_ms: Callable[[], int] | None = None,
        allow_non_durable_for_tests: bool = False,
    ):
        self.client = client or BloFinDemoSpotReadOnly()
        self.connection_factory = connection_factory
        self.backend_factory = backend_factory
        self.now_ms = now_ms or (lambda: int(time.time() * 1000))
        self.allow_non_durable_for_tests = bool(allow_non_durable_for_tests)

    def _record_unknown_run(
        self, memory: LearningExperienceMemory, connection: Any,
        *, round_number: int, reason_code: str,
    ) -> dict[str, Any]:
        run_id = hashlib.sha256(
            f"{_round_prefix(round_number)}:{reason_code}:{self.now_ms()}".encode()
        ).hexdigest()[:24]
        experience_id = f"ch01-unknown-{run_id}"
        cycle_id = f"{_round_prefix(round_number)}:UNKNOWN:{run_id}"
        with connection:
            memory.record_decision(
                experience_id=experience_id,
                event_id=f"{experience_id}:decision",
                engine_id=ENGINE_ID,
                challenge_id=CHALLENGE_ID,
                cycle_id=cycle_id,
                decision_mode="BOT_ONLY",
                environment="BLOFIN_DEMO",
                market="SPOT",
                symbol=SYMBOL,
                timeframe=TIMEFRAME,
                model_version="ch01-rule-v1",
                dataset_version=f"{_round_prefix(round_number)}:UNAVAILABLE",
                decision={
                    "candle_classification": "UNKNOWN",
                    "action": "NO_SIGNAL",
                    "reason_code": reason_code,
                },
                input_snapshot={
                    "source": "blofin_demo_spot_public_candles",
                    "round_number": round_number,
                    "data_quality": "UNKNOWN",
                },
            )
            memory.record_outcome(
                experience_id=experience_id,
                event_id=f"{experience_id}:outcome",
                decision_correctness="UNKNOWN",
                realized_result="NOT_EXECUTED",
                realized_net_pnl=None,
                final=True,
                evidence={
                    "source": "challenge_01_data_validation",
                    "reason_code": reason_code,
                    "orders_submitted": False,
                },
            )
        return {
            "status": "UNKNOWN",
            "reason_code": reason_code,
            "experience_id": experience_id,
            "orders_submitted": False,
        }

    def run_bot_only_round(self, *, round_number: int = 1, sample_count: int = 100) -> dict[str, Any]:
        if isinstance(round_number, bool) or not isinstance(round_number, int) or not 1 <= round_number <= MAX_ROUNDS:
            raise ValueError("round_number must be between 1 and 5")
        if isinstance(sample_count, bool) or not isinstance(sample_count, int) or sample_count < MIN_SAMPLES_PER_ROUND:
            raise ValueError("Challenge 1 requires at least 100 samples per round")
        if sample_count > 1400:
            raise ValueError("sample_count must not exceed 1400")
        try:
            backend = self.backend_factory()
        except Exception as exc:
            return {
                "status": "BLOCKED",
                "reason_code": "LEARNING_MEMORY_BACKEND_UNAVAILABLE",
                "error_type": type(exc).__name__,
                "orders_submitted": False,
                "live_trading": False,
            }
        if backend != "TURSO_CLOUD_SQLITE" and not self.allow_non_durable_for_tests:
            return {
                "status": "BLOCKED",
                "reason_code": "DURABLE_LEARNING_MEMORY_REQUIRED",
                "memory_backend": backend,
                "orders_submitted": False,
                "live_trading": False,
            }

        connection = self.connection_factory()
        try:
            memory = LearningExperienceMemory(connection)
            prior_rounds = _existing_rounds(memory)
            expected_round = max(prior_rounds, default=0) + 1
            if round_number > 1:
                reason = (
                    "BOT_ONLY_BASELINE_REQUIRED"
                    if expected_round == 1 else "AI_MENTOR_PHASE_REQUIRED"
                )
                return {
                    "status": "BLOCKED",
                    "reason_code": reason,
                    "blocked_round": round_number,
                    "expected_round": expected_round,
                    "next_stage": "BOT_ONLY_BASELINE" if expected_round == 1 else "AI_MENTOR_REQUIRED",
                    "orders_submitted": False,
                    "live_trading": False,
                }
            if round_number != expected_round:
                return {
                    "status": "BLOCKED",
                    "reason_code": "BOT_ONLY_BASELINE_ALREADY_LOCKED",
                    "expected_round": expected_round,
                    "next_stage": "AI_MENTOR_REQUIRED",
                    "orders_submitted": False,
                    "live_trading": False,
                }
            after = None
            if round_number > 1:
                previous_times = prior_rounds.get(round_number - 1, [])
                if len(previous_times) < MIN_SAMPLES_PER_ROUND:
                    raise Challenge01DataError("PRIOR_ROUND_SAMPLE_INSUFFICIENT")
                after = min(previous_times)

            try:
                rows = self.client.get_candles(
                    SYMBOL, bar=TIMEFRAME, limit=min(sample_count + CONTEXT_WINDOW_LENGTH, 1440), after=after,
                )
                if not isinstance(rows, list) or not rows:
                    raise Challenge01DataError("EMPTY_CANDLE_RESPONSE")
                parsed = [_parse_candle(row) for row in rows]
                timestamps = [item["open_time_ms"] for item in parsed]
                if len(set(timestamps)) != len(timestamps):
                    raise Challenge01DataError("DUPLICATE_CANDLE_TIMESTAMP")
                if any(timestamps[index] <= timestamps[index + 1] for index in range(len(timestamps) - 1)):
                    raise Challenge01DataError("CANDLE_ORDER_NOT_NEWEST_FIRST")
                completed = [item for item in parsed if item["confirmed"]]
                # A one-bar-ahead sample needs one completed feature candle and
                # one later completed target candle for every prediction.
                if len(completed) < sample_count + CONTEXT_WINDOW_LENGTH:
                    raise Challenge01DataError("INSUFFICIENT_COMPLETED_CANDLES_FOR_50_BAR_CONTEXT_WINDOW")
                # Each prediction uses 50 completed context candles plus
                # one later completed target; adjacent contexts may overlap safely.
                selected_desc = completed[-(sample_count + CONTEXT_WINDOW_LENGTH):]
                selected = list(reversed(selected_desc))
                for index in range(len(selected) - 1):
                    if selected[index + 1]["open_time_ms"] - selected[index]["open_time_ms"] != BAR_MILLISECONDS:
                        raise Challenge01DataError("NON_CONTIGUOUS_FEATURE_TARGET_CANDLES")
                if after is None:
                    # Validate freshness against the newest completed market bar,
                    # not against the older chronological training block.
                    newest_close_ms = completed[0]["open_time_ms"] + BAR_MILLISECONDS
                    age_ms = self.now_ms() - newest_close_ms
                    if age_ms < -5 * 60 * 1000:
                        raise Challenge01DataError("LATEST_CANDLE_CLOSE_IN_FUTURE")
                    if age_ms > MAX_STALE_AFTER_CLOSE_MS:
                        raise Challenge01DataError("LATEST_COMPLETED_CANDLE_STALE")
                if after is not None and any(item["open_time_ms"] >= int(after) for item in selected):
                    raise Challenge01DataError("ROUND_DATA_OVERLAP")
            except Challenge01DataError as exc:
                result = self._record_unknown_run(
                    memory, connection, round_number=round_number, reason_code=str(exc),
                )
                result["memory_backend"] = self.backend_factory()
                return result
            except Exception as exc:
                result = self._record_unknown_run(
                    memory, connection, round_number=round_number,
                    reason_code=f"PROVIDER_{type(exc).__name__.upper()}",
                )
                result["memory_backend"] = self.backend_factory()
                return result

            samples = []
            for index in range(sample_count):
                context_window = selected[index:index + CONTEXT_WINDOW_LENGTH]
                feature_window = context_window[-FEATURE_WINDOW_LENGTH:]
                samples.append({
                    "context_window": context_window,
                    "feature_window": feature_window,
                    "feature": feature_window[-1],
                    # Store only the expected target time/index. Do not read target OHLC
                    # until this sample's immutable BOT_ONLY decision has been appended.
                    "target_index": index + CONTEXT_WINDOW_LENGTH,
                    "target_open_time_ms": context_window[-1]["open_time_ms"] + BAR_MILLISECONDS,
                })
            # The dataset version is derived only from information available at
            # prediction time. Future target OHLC must not influence even its hash.
            dataset_canonical = json.dumps(
                [
                    [
                        [candle["open_time_ms"] for candle in sample["context_window"]],
                        [[candle["open"], candle["high"], candle["low"], candle["close"], candle.get("volume", 0)]
                         for candle in sample["context_window"]],
                        sample["target_open_time_ms"],
                    ]
                    for sample in samples
                ],
                separators=(",", ":"), sort_keys=False,
            )
            dataset_hash = hashlib.sha256(dataset_canonical.encode("utf-8")).hexdigest()
            dataset_version = f"{_round_prefix(round_number)}:{dataset_hash[:16]}"
            inserted_decisions = 0
            inserted_outcomes = 0
            predictions: list[str] = []
            references: list[str] = []
            first_experience_id = None
            with connection:
                for sample in samples:
                    feature_candle = sample["feature"]
                    feature_ts = feature_candle["open_time_ms"]
                    target_ts = sample["target_open_time_ms"]
                    if target_ts != feature_ts + BAR_MILLISECONDS:
                        raise Challenge01DataError("FEATURE_TARGET_NOT_ONE_BAR_APART")
                    experience_id = f"ch01-r{round_number:02d}-btc-usdt-1h-{target_ts}"
                    first_experience_id = first_experience_id or experience_id
                    prediction, action = _classify_bot(feature_candle)
                    decision_event = memory.record_decision(
                        experience_id=experience_id,
                        event_id=f"{experience_id}:decision",
                        engine_id=ENGINE_ID,
                        challenge_id=CHALLENGE_ID,
                        cycle_id=f"{_round_prefix(round_number)}:{SYMBOL}:{TIMEFRAME}:{target_ts}",
                        decision_mode="BOT_ONLY",
                        environment="BLOFIN_DEMO",
                        market="SPOT",
                        symbol=SYMBOL,
                        timeframe=TIMEFRAME,
                        model_version="ch01-momentum-baseline-v2",
                        dataset_version=dataset_version,
                        decision={
                            "candle_classification": prediction,
                            "predicted_next_candle_classification": prediction,
                            "action": action,
                            "confidence": None,
                            "confidence_scope": "UNSCORED_BASELINE_RULE_NOT_CALIBRATED_PROBABILITY",
                            "rule": "previous_candle_body_direction",
                            "prediction_horizon_bars": 1,
                            "reason": f"feature_close={feature_candle['close']}; feature_open={feature_candle['open']}",
                        },
                        input_snapshot={
                            "source": "blofin_demo_spot_public_candles",
                            "round_number": round_number,
                            # Compatibility key now identifies the target timestamp.
                            "candle_open_time_ms": target_ts,
                            "feature_candle_open_time_ms": feature_ts,
                            "target_candle_open_time_ms": target_ts,
                            "feature_candle": feature_candle,
                            "feature_window": sample["feature_window"],
                            "context_window": sample["context_window"],
                            "context_window_open_times_ms": [
                                candle["open_time_ms"] for candle in sample["context_window"]
                            ],
                            "feature_window_open_times_ms": [
                                candle["open_time_ms"] for candle in sample["feature_window"]
                            ],
                            "detected_candlestick_patterns": detect_candlestick_patterns(
                                sample["feature_window"]
                            ),
                            "pattern_context": build_pattern_context(sample["context_window"]),
                            "prediction_horizon_bars": 1,
                            "data_quality": "VALID_COMPLETED_FEATURE_AND_TARGET",
                            "target_candle_in_input": False,
                        },
                    )
                    inserted_decisions += int(bool(decision_event.get("inserted")))

                    # The BOT_ONLY prediction is now immutable in the journal.
                    # Only at this point inspect the future candle's OHLC and label it.
                    target_candle = selected[sample["target_index"]]
                    if (
                        target_candle.get("open_time_ms") != target_ts
                        or target_candle.get("confirmed") is not True
                    ):
                        raise Challenge01DataError("TARGET_CANDLE_NOT_COMPLETED_OR_TIME_MISMATCH")
                    reference = _reference_label(target_candle)
                    predictions.append(prediction)
                    references.append(reference)
                    outcome_event = memory.record_outcome(
                        experience_id=experience_id,
                        event_id=f"{experience_id}:outcome",
                        decision_correctness="CORRECT" if prediction == reference else "INCORRECT",
                        realized_result="NOT_EXECUTED",
                        realized_net_pnl=None,
                        final=True,
                        evidence={
                            "source": "blofin_demo_spot_next_completed_candle",
                            "reference_classification": reference,
                            "comparison": "NEXT_CANDLE_DIRECTION_FROM_NEXT_COMPLETED_BAR",
                            "feature_candle_open_time_ms": feature_ts,
                            "feature_window_open_times_ms": [
                                candle["open_time_ms"] for candle in sample["feature_window"]
                            ],
                            "context_window_open_times_ms": [
                                candle["open_time_ms"] for candle in sample["context_window"]
                            ],
                            "target_candle_open_time_ms": target_ts,
                            "target_candle": target_candle,
                            "prediction_horizon_bars": 1,
                            "no_future_data_used_in_input": True,
                            "orders_submitted": False,
                            "not_a_trade_outcome": True,
                        },
                    )
                    inserted_outcomes += int(bool(outcome_event.get("inserted")))

            connection.close()
            connection = self.connection_factory()
            recovered_memory = LearningExperienceMemory(connection)
            verified = 0
            for sample in samples:
                target_ts = sample["target_open_time_ms"]
                experience_id = f"ch01-r{round_number:02d}-btc-usdt-1h-{target_ts}"
                recovered = recovered_memory.get_experience(experience_id)
                if recovered["verification"]["status"] != "PASS":
                    raise RuntimeError("LEARNING_MEMORY_INTEGRITY_FAILED")
                if recovered["learning_status"] != "OUTCOME_RECORDED":
                    raise RuntimeError("LEARNING_MEMORY_LIFECYCLE_INCOMPLETE")
                verified += 1

            metrics = _classification_metrics(predictions, references)
            return {
                "status": "BASELINE_RECORDED",
                "challenge_id": CHALLENGE_ID,
                "engine_id": ENGINE_ID,
                "round_number": round_number,
                "round_status": "FUNCTIONAL_BASELINE_RECORDED_NOT_MASTERY",
                "decision_mode": "BOT_ONLY",
                "environment": "BLOFIN_DEMO",
                "market": "SPOT",
                "symbol": SYMBOL,
                "timeframe": TIMEFRAME,
                "sample_count": len(samples),
                "dataset_version": dataset_version,
                "dataset_sha256": dataset_hash,
                "prediction_task": "NEXT_CANDLE_DIRECTION",
                "prediction_horizon_bars": 1,
                "classification_rule": "previous_candle_body_direction",
                "data_split": "CHRONOLOGICAL_FEATURE_TO_NEXT_BAR_TARGET",

                "balanced_accuracy": metrics["balanced_accuracy"],
                "macro_f1": metrics["macro_f1"],
                "signal_precision": metrics["signal_precision"],
                "per_class_metrics": metrics["per_class"],
                "confusion_matrix": metrics["confusion_matrix"],
                "metrics_scope": metrics["metric_scope"],
                "decision_correctness_scope": "NEXT_CANDLE_DIRECTION_ONE_BAR_AHEAD",
                "trade_outcome": "NOT_EXECUTED",
                "inserted_decisions": inserted_decisions,
                "inserted_outcomes": inserted_outcomes,
                "memory_recovery_verified": verified == len(samples),
                "memory_records_verified": verified,
                "memory_integrity": "PASS",
                "memory_backend": backend,
                "next_stage": "AI_MENTOR_REQUIRED",
                "baseline_locked": True,
                "orders_submitted": False,
                "live_trading": False,
                "first_experience_id": first_experience_id,
                "example_experience_ids": [
                    f"ch01-r{round_number:02d}-btc-usdt-1h-{item['target_open_time_ms']}"
                    for item in samples[:3]
                ],
            }
        finally:
            try:
                connection.close()
            except Exception:
                pass



def get_challenge_01_status(
    *,
    connection_factory: Callable[[], Any] = connect_database,
    backend_factory: Callable[[], str] = storage_backend,
    allow_non_durable_for_tests: bool = False,
) -> dict[str, Any]:
    """Read verified Challenge 1 progress and the next safe round gate."""
    connection = None
    try:
        backend = backend_factory()
        if backend != "TURSO_CLOUD_SQLITE" and not allow_non_durable_for_tests:
            return {
                "status": "BLOCKED",
                "reason_code": "DURABLE_LEARNING_MEMORY_REQUIRED",
                "challenge_id": CHALLENGE_ID,
                "engine_id": ENGINE_ID,
                "memory_backend": backend,
                "verified_experiences": 0,
                "completed_rounds": [],
                "next_round": None,
                "mastery_status": "NOT_EARNED",
                "orders_submitted": False,
            }

        connection = connection_factory()
        memory = LearningExperienceMemory(connection)
        experiences = memory.recent_experiences(limit=1200, challenge_id=CHALLENGE_ID)
        round_targets: dict[int, dict[int, set[str]]] = {}
        mentor_reviewed_ids: set[str] = set()
        for item in experiences:
            events = item.get("events") or []
            decision_event = next(
                (event for event in events if event.get("event_type") == "DECISION_RECORDED"),
                None,
            )
            if decision_event is None:
                continue
            payload = decision_event.get("payload") or {}
            snapshot = payload.get("input_snapshot") or {}
            round_number = snapshot.get("round_number")
            target_time = snapshot.get("target_candle_open_time_ms")
            if not isinstance(target_time, int):
                target_time = snapshot.get("candle_open_time_ms")
            if not isinstance(round_number, int) or not isinstance(target_time, int):
                continue
            mode = item.get("decision_mode")
            round_targets.setdefault(round_number, {}).setdefault(target_time, set()).add(mode)

            if round_number == 1 and mode == "BOT_ONLY":
                if any(event.get("event_type") == "MENTOR_FEEDBACK_RECORDED" for event in events):
                    mentor_reviewed_ids.add(item.get("experience_id"))

        completed_rounds = []
        paired_rounds: dict[int, bool] = {}
        for number, target_modes in sorted(round_targets.items()):
            target_times = set(target_modes)
            bot_samples = sum("BOT_ONLY" in modes for modes in target_modes.values())
            mentor_samples = sum("BOT_WITH_AI_MENTOR" in modes for modes in target_modes.values())
            paired_samples = sum(
                {"BOT_ONLY", "BOT_WITH_AI_MENTOR"}.issubset(modes)
                for modes in target_modes.values()
            )
            if number == 1:
                ready = bot_samples >= MIN_SAMPLES_PER_ROUND
                paired_rounds[number] = ready
            else:
                ready = paired_samples >= MIN_SAMPLES_PER_ROUND
                paired_rounds[number] = ready
            completed_rounds.append({
                "round_number": number,
                "verified_samples": len(target_times),
                "bot_only_samples": bot_samples,
                "mentor_samples": mentor_samples,
                "paired_samples": paired_samples,
                "round_ready": ready,
            })

        baseline_samples = sum(
            "BOT_ONLY" in modes
            for modes in round_targets.get(1, {}).values()
        )
        baseline_locked = baseline_samples >= MIN_SAMPLES_PER_ROUND
        mentor_reviewed_samples = len(mentor_reviewed_ids)
        mentor_review_complete = (
            baseline_locked and mentor_reviewed_samples >= baseline_samples
        )

        candidate_model_sha256 = None
        candidate_model_through_round = 0
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
                    trained_round = artifact.get("trained_through_round")
                    total_samples = artifact.get("training", {}).get("total_samples")
                    if (
                        saved_hash == calculated_hash
                        and artifact.get("schema") == "CSR_CH01_CANDLE_PATTERN_PA_SOFTMAX_V4"
                        and artifact.get("feature_window_length") == FEATURE_WINDOW_LENGTH
                        and isinstance(artifact.get("pattern_features"), list)
                        and len(artifact.get("pattern_features")) >= 15
                        and isinstance(artifact.get("pattern_catalog"), dict)
                        and len(artifact.get("pattern_catalog")) == len(artifact.get("pattern_features"))
                        and artifact.get("pattern_catalog_version") == "CSR_CANDLE_HEURISTICS_5BAR_V2"
                        and artifact.get("market_context_version") == MARKET_CONTEXT_VERSION
                        and artifact.get("market_context_scope") == "AUDIT_AND_REGIME_STRATIFICATION_ONLY_NOT_MODEL_FEATURES"
                        and artifact.get("context_window_length") == CONTEXT_WINDOW_LENGTH
                        and isinstance(artifact.get("pattern_feature_mapping"), dict)
                        and len(artifact.get("pattern_feature_mapping")) == len(artifact.get("pattern_features", []))
                        and artifact.get("promotion_status") == "CANDIDATE_NOT_PROMOTED"
                        and isinstance(trained_round, int)
                        and artifact.get("training", {}).get("samples", 0) >= 60
                        and artifact.get("temporal_validation", {}).get("samples", 0) >= 20
                        and total_samples == artifact.get("training", {}).get("samples", 0)
                        + artifact.get("temporal_validation", {}).get("samples", 0)
                        and trained_round >= candidate_model_through_round
                    ):
                        candidate_model_sha256 = saved_hash
                        candidate_model_through_round = trained_round
                except (TypeError, ValueError, KeyError):
                    continue

        candidate_model_present = candidate_model_sha256 is not None
        highest_completed_round = 1 if baseline_locked and mentor_review_complete else 0
        for number in range(2, MAX_ROUNDS + 1):
            if paired_rounds.get(number, False) and highest_completed_round == number - 1:
                highest_completed_round = number
            else:
                break
        # Round 5 is the final holdout: it consumes the candidate trained
        # after round 4. No round-5 candidate is needed because there is no
        # round 6. Earlier completed stages require a candidate through their
        # own round so it can drive the next stage.
        required_candidate_through_round = min(highest_completed_round, MAX_ROUNDS - 1)
        candidate_model_ready = (
            candidate_model_present
            and candidate_model_through_round == required_candidate_through_round
            and highest_completed_round >= 1
        )
        mentor_review_ready = baseline_locked and not mentor_review_complete
        model_build_required = (
            baseline_locked and mentor_review_complete and not candidate_model_ready
        )
        if not baseline_locked:
            next_round = 1
            next_stage = "BOT_ONLY_BASELINE"
        elif not mentor_review_complete:
            next_round = None
            next_stage = "AI_MENTOR_REQUIRED"
        elif not candidate_model_ready:
            next_round = None
            next_stage = "AI_MENTOR_MODEL_BUILD_REQUIRED"
        else:
            next_round = next(
                (number for number in range(2, MAX_ROUNDS + 1)
                 if not paired_rounds.get(number, False)),
                None,
            )
            next_stage = (
                "FORWARD_HOLDOUT_VALIDATION_REQUIRED"
                if next_round is not None else "CHALLENGE_REVIEW_REQUIRED"
            )
        return {
            "status": "PASS",
            "challenge_id": CHALLENGE_ID,
            "engine_id": ENGINE_ID,
            "environment": "BLOFIN_DEMO",
            "market": "SPOT",
            "symbol": SYMBOL,
            "timeframe": TIMEFRAME,
            "decision_mode": "BOT_ONLY",
            "memory_backend": backend,
            "verified_experiences": len(experiences),
            "completed_rounds": completed_rounds,
            "baseline_samples": baseline_samples,
            "baseline_locked": baseline_locked,
            "mentor_reviewed_samples": mentor_reviewed_samples,
            "mentor_review_required_samples": baseline_samples if baseline_locked else MIN_SAMPLES_PER_ROUND,
            "mentor_review_ready": mentor_review_ready,
            "mentor_review_complete": mentor_review_complete,
            "candidate_model_present": candidate_model_present,
            "candidate_model_sha256": candidate_model_sha256,
            "candidate_model_through_round": candidate_model_through_round,
            "candidate_model_ready": candidate_model_ready,
            "model_build_required": model_build_required,
            "highest_completed_round": highest_completed_round,
            "next_round": next_round,
            "next_stage": next_stage,
            "later_rounds_blocked": next_round is None,
            "mastery_status": "NOT_EARNED",
            "mastery_rule": "BOT_ONLY baseline, 100% mentor audit, then four paired independent assisted rounds; this endpoint never awards mastery",
            "orders_submitted": False,
        }
    except Exception as exc:
        return {
            "status": "UNKNOWN",
            "error_type": type(exc).__name__,
            "orders_submitted": False,
        }
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


__all__ = ["Challenge01Runner", "Challenge01DataError", "get_challenge_01_status"]

