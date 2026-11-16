"""Append-only learning-experience memory for CRYPTO SUPER RADAR.

This store records bot decisions, final outcomes, mentor feedback and tested
lessons. It reuses the application's DB-API connection (SQLite or configured
Turso). It never connects to an exchange or submits orders.

The caller owns transaction boundaries. Wrap each logical write in the same
database transaction as related application records.
"""
from __future__ import annotations

import hashlib
import json
import math
import threading
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

DECISION_MODES = frozenset({"BOT_ONLY", "BOT_WITH_AI_MENTOR"})
ENVIRONMENTS = frozenset({"HISTORICAL_REPLAY", "PAPER", "BLOFIN_DEMO"})
EVENT_TYPES = frozenset({
    "DECISION_RECORDED",
    "OUTCOME_LABELLED",
    "MENTOR_FEEDBACK_RECORDED",
    "LESSON_PROPOSED",
    "LESSON_VALIDATION_RECORDED",
})
DECISION_CORRECTNESS = frozenset({"CORRECT", "INCORRECT", "UNKNOWN"})
REALIZED_RESULTS = frozenset({"PROFIT", "LOSS", "BREAKEVEN", "NOT_EXECUTED"})
SENSITIVE_KEYS = frozenset({
    "password", "passwd", "secret", "api_key", "apikey", "api_secret",
    "auth_token", "api_token", "access_token", "refresh_token", "client_secret",
    "secret_key", "token", "passphrase", "private_key", "seed_phrase",
    "wallet_seed", "authorization", "bearer_token",
})


class ExperienceMemoryError(ValueError):
    """Base error for invalid learning-memory operations."""


class ExperienceIdempotencyConflict(ExperienceMemoryError):
    """An event ID was reused with different immutable content."""


class ExperienceIntegrityError(ExperienceMemoryError):
    """Persisted learning history failed integrity verification."""


class ExperienceStateError(ExperienceMemoryError):
    """An event is invalid for the current experience lifecycle."""


class ExperienceNotFoundError(KeyError):
    """The requested experience does not exist."""


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExperienceMemoryError(f"{field} must be a non-empty string")
    return value.strip()


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ExperienceMemoryError(
            "payload must contain finite JSON-compatible data"
        ) from exc


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _timestamp_utc(value: str | None = None) -> str:
    if value is None:
        parsed = datetime.now(timezone.utc)
    else:
        if not isinstance(value, str) or not value.strip():
            raise ExperienceMemoryError("timestamp_utc must be a non-empty ISO-8601 string")
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise ExperienceMemoryError(
                "timestamp_utc must be a valid ISO-8601 timestamp"
            ) from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ExperienceMemoryError("timestamp_utc must include a timezone")
        parsed = parsed.astimezone(timezone.utc)
    return parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _reject_secrets(value: Any, path: str = "payload") -> None:
    """Reject common credential fields before a snapshot is persisted."""
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).strip().lower().replace("-", "_").replace(" ", "_")
            if normalized in SENSITIVE_KEYS:
                raise ExperienceMemoryError(
                    f"sensitive field is not allowed in {path}.{key}"
                )
            _reject_secrets(child, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_secrets(child, f"{path}[{index}]")


def _finite_pnl(value: Any) -> float:
    if isinstance(value, bool) or value is None:
        raise ExperienceMemoryError("realized_net_pnl must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ExperienceMemoryError(
            "realized_net_pnl must be a finite number"
        ) from exc
    if not math.isfinite(number):
        raise ExperienceMemoryError("realized_net_pnl must be a finite number")
    return number


class LearningExperienceMemory:
    """Append-only event journal for decisions and learning outcomes.

    The supplied connection must be the application's configured database
    connection. For durable production storage, configure the existing durable
    backend; a local file on an ephemeral container is not durable across
    container replacement.
    """

    def __init__(self, connection: Any, *, lock: threading.RLock | None = None):
        if connection is None or not callable(getattr(connection, "execute", None)):
            raise ValueError("a DB-API connection with execute() is required")
        self._db = connection
        self._lock = lock or threading.RLock()
        with self._lock:
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS learning_experience_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    experience_id TEXT NOT NULL,
                    experience_sequence INTEGER NOT NULL CHECK(experience_sequence >= 1),
                    engine_id TEXT NOT NULL,
                    challenge_id TEXT NOT NULL,
                    cycle_id TEXT NOT NULL,
                    decision_mode TEXT NOT NULL,
                    environment TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    timestamp_utc TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    previous_event_hash TEXT,
                    event_hash TEXT NOT NULL,
                    UNIQUE(experience_id, experience_sequence)
                )"""
            )
            self._db.execute(
                """CREATE INDEX IF NOT EXISTS idx_learning_experience_challenge
                   ON learning_experience_events(challenge_id, sequence)"""
            )
            self._db.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_learning_experience_cycle_mode
                   ON learning_experience_events(engine_id, challenge_id, cycle_id, decision_mode)
                   WHERE experience_sequence = 1"""
            )
            self._db.execute(
                """CREATE INDEX IF NOT EXISTS idx_learning_experience_cycle
                   ON learning_experience_events(challenge_id, cycle_id, engine_id, sequence)"""
            )
            self._db.execute(
                """CREATE TRIGGER IF NOT EXISTS learning_experience_no_update
                   BEFORE UPDATE ON learning_experience_events
                   BEGIN SELECT RAISE(ABORT, 'learning experience events are append-only'); END"""
            )
            self._db.execute(
                """CREATE TRIGGER IF NOT EXISTS learning_experience_no_delete
                   BEFORE DELETE ON learning_experience_events
                   BEGIN SELECT RAISE(ABORT, 'learning experience events are append-only'); END"""
            )

    @staticmethod
    def _as_dict(row: Any) -> dict[str, Any]:
        return dict(row)

    @staticmethod
    def _event_hash_payload(event: Mapping[str, Any]) -> str:
        fields = {
            "event_id": event["event_id"],
            "experience_id": event["experience_id"],
            "experience_sequence": int(event["experience_sequence"]),
            "engine_id": event["engine_id"],
            "challenge_id": event["challenge_id"],
            "cycle_id": event["cycle_id"],
            "decision_mode": event["decision_mode"],
            "environment": event["environment"],
            "event_type": event["event_type"],
            "timestamp_utc": event["timestamp_utc"],
            "payload_json": event["payload_json"],
            "payload_hash": event["payload_hash"],
            "previous_event_hash": event["previous_event_hash"],
        }
        return _sha256(_canonical_json(fields))

    def record_decision(
        self,
        *,
        experience_id: str,
        event_id: str,
        engine_id: str,
        challenge_id: str,
        cycle_id: str,
        decision_mode: str,
        environment: str,
        decision: Mapping[str, Any],
        input_snapshot: Mapping[str, Any],
        market: str = "UNKNOWN",
        symbol: str = "UNKNOWN",
        timeframe: str = "UNKNOWN",
        model_version: str = "UNKNOWN",
        dataset_version: str = "UNKNOWN",
        baseline_experience_id: str | None = None,
        timestamp_utc: str | None = None,
    ) -> dict[str, Any]:
        """Record one immutable BOT_ONLY or mentor-assisted decision."""
        experience_key = _required_text(experience_id, "experience_id")
        mode = _required_text(decision_mode, "decision_mode").upper()
        environment_value = _required_text(environment, "environment").upper()
        if mode not in DECISION_MODES:
            raise ExperienceMemoryError(
                f"decision_mode must be one of {sorted(DECISION_MODES)}"
            )
        if environment_value not in ENVIRONMENTS:
            raise ExperienceMemoryError(
                f"environment must be one of {sorted(ENVIRONMENTS)}"
            )
        metadata = {
            "engine_id": _required_text(engine_id, "engine_id"),
            "challenge_id": _required_text(challenge_id, "challenge_id"),
            "cycle_id": _required_text(cycle_id, "cycle_id"),
            "decision_mode": mode,
            "environment": environment_value,
        }
        if not isinstance(decision, Mapping) or not isinstance(input_snapshot, Mapping):
            raise ExperienceMemoryError("decision and input_snapshot must be objects")
        baseline_id = (
            _required_text(baseline_experience_id, "baseline_experience_id")
            if baseline_experience_id is not None else None
        )
        if mode == "BOT_ONLY" and baseline_id is not None:
            raise ExperienceMemoryError("BOT_ONLY must not reference a mentor baseline")
        if mode == "BOT_WITH_AI_MENTOR":
            if baseline_id is None:
                raise ExperienceMemoryError(
                    "BOT_WITH_AI_MENTOR requires baseline_experience_id"
                )
            baseline = self.get_experience(baseline_id)
            baseline_types = [item["event_type"] for item in baseline["events"]]
            baseline_decision = next(
                (item.get("payload", {}) for item in baseline["events"]
                 if item["event_type"] == "DECISION_RECORDED"),
                {},
            )
            baseline_snapshot = baseline_decision.get("input_snapshot") or {}
            assisted_snapshot = dict(input_snapshot)
            same_cycle = baseline["cycle_id"] == metadata["cycle_id"]
            if not same_cycle:
                baseline_round = baseline_snapshot.get("round_number")
                assisted_round = assisted_snapshot.get("round_number")
                baseline_target_time = baseline_snapshot.get("target_candle_open_time_ms")
                if baseline_target_time is None:
                    baseline_target_time = baseline_snapshot.get("candle_open_time_ms")
                assisted_feature_time = assisted_snapshot.get("feature_candle_open_time_ms")
                assisted_target_time = assisted_snapshot.get("target_candle_open_time_ms")
                temporal_transfer_valid = (
                    isinstance(baseline_round, int)
                    and isinstance(assisted_round, int)
                    and baseline_round < assisted_round
                    and isinstance(baseline_target_time, int)
                    and isinstance(assisted_feature_time, int)
                    and isinstance(assisted_target_time, int)
                    and baseline_target_time <= assisted_feature_time < assisted_target_time
                )
            else:
                # Preserve same-cycle behaviour for existing integrations.
                temporal_transfer_valid = True
            if (
                baseline["decision_mode"] != "BOT_ONLY"
                or baseline["engine_id"] != metadata["engine_id"]
                or baseline["challenge_id"] != metadata["challenge_id"]
                or not temporal_transfer_valid
                or "OUTCOME_LABELLED" not in baseline_types
                or "MENTOR_FEEDBACK_RECORDED" not in baseline_types
            ):
                raise ExperienceStateError(
                    "mentor-assisted decision requires a verified final-labelled BOT_ONLY "
                    "baseline with Mentor feedback from the same challenge/engine and "
                    "either the same cycle or a strictly earlier round whose target is "
                    "available before the assisted feature timestamp"
                )
        payload = {
            **metadata,
            "market": _required_text(market, "market").upper(),
            "symbol": _required_text(symbol, "symbol").upper(),
            "timeframe": _required_text(timeframe, "timeframe").upper(),
            "model_version": _required_text(model_version, "model_version"),
            "dataset_version": _required_text(dataset_version, "dataset_version"),
            "baseline_experience_id": baseline_id,
            "decision": dict(decision),
            "input_snapshot": dict(input_snapshot),
        }
        _reject_secrets(payload)
        return self._append(
            experience_id=experience_key,
            event_id=event_id,
            event_type="DECISION_RECORDED",
            metadata=metadata,
            payload=payload,
            timestamp_utc=timestamp_utc,
        )

    def record_outcome(
        self,
        *,
        experience_id: str,
        event_id: str,
        decision_correctness: str,
        realized_result: str,
        realized_net_pnl: Any = None,
        pnl_currency: str = "USDT",
        final: bool = True,
        evidence: Mapping[str, Any],
        timestamp_utc: str | None = None,
    ) -> dict[str, Any]:
        """Append a final label only; open or mark-to-market PnL is rejected."""
        correctness = _required_text(
            decision_correctness, "decision_correctness"
        ).upper()
        result = _required_text(realized_result, "realized_result").upper()
        if correctness not in DECISION_CORRECTNESS:
            raise ExperienceMemoryError(
                f"decision_correctness must be one of {sorted(DECISION_CORRECTNESS)}"
            )
        if result not in REALIZED_RESULTS:
            raise ExperienceMemoryError(
                f"realized_result must be one of {sorted(REALIZED_RESULTS)}"
            )
        if final is not True:
            raise ExperienceStateError("only final outcomes can be stored as training labels")
        if not isinstance(evidence, Mapping) or not evidence:
            raise ExperienceMemoryError("outcome evidence must be a non-empty object")
        evidence_copy = dict(evidence)
        _required_text(evidence_copy.get("source"), "evidence.source")
        _reject_secrets(evidence_copy)

        if result == "NOT_EXECUTED":
            if realized_net_pnl is not None:
                raise ExperienceMemoryError(
                    "NOT_EXECUTED must not contain realized_net_pnl"
                )
            pnl = None
            case = "NOT_EXECUTED"
        else:
            pnl = _finite_pnl(realized_net_pnl)
            if result == "PROFIT" and pnl <= 0:
                raise ExperienceMemoryError("PROFIT requires realized_net_pnl > 0")
            if result == "LOSS" and pnl >= 0:
                raise ExperienceMemoryError("LOSS requires realized_net_pnl < 0")
            if result == "BREAKEVEN" and not math.isclose(pnl, 0.0, abs_tol=1e-8):
                raise ExperienceMemoryError(
                    "BREAKEVEN requires realized_net_pnl equal to zero"
                )
            if correctness == "UNKNOWN":
                case = "UNCLASSIFIED_" + result
            elif correctness == "INCORRECT" and result == "PROFIT":
                case = "INCORRECT_PROFIT_LUCK"
            else:
                case = correctness + "_" + result

        payload = {
            "final": True,
            "decision_correctness": correctness,
            "realized_result": result,
            "realized_net_pnl": pnl,
            "pnl_currency": _required_text(pnl_currency, "pnl_currency").upper(),
            "experience_case": case,
            "evidence": evidence_copy,
        }
        return self._append(
            experience_id=experience_id,
            event_id=event_id,
            event_type="OUTCOME_LABELLED",
            payload=payload,
            timestamp_utc=timestamp_utc,
        )

    def record_mentor_feedback(
        self,
        *,
        experience_id: str,
        event_id: str,
        mentor_model_version: str,
        feedback: str,
        proposed_change: str = "",
        evidence: Mapping[str, Any] | None = None,
        timestamp_utc: str | None = None,
    ) -> dict[str, Any]:
        """Record mentor feedback only after the BOT_ONLY result is locked."""
        payload = {
            "mentor_model_version": _required_text(
                mentor_model_version, "mentor_model_version"
            ),
            "feedback": _required_text(feedback, "feedback"),
            "proposed_change": str(proposed_change or "").strip(),
            "evidence": dict(evidence or {}),
        }
        _reject_secrets(payload)
        return self._append(
            experience_id=experience_id,
            event_id=event_id,
            event_type="MENTOR_FEEDBACK_RECORDED",
            payload=payload,
            timestamp_utc=timestamp_utc,
        )

    def propose_lesson(
        self,
        *,
        experience_id: str,
        event_id: str,
        lesson_id: str,
        lesson: str,
        evidence_event_ids: list[str],
        timestamp_utc: str | None = None,
    ) -> dict[str, Any]:
        """Store a candidate lesson; it remains untrusted until independent tests."""
        if not isinstance(evidence_event_ids, list) or not evidence_event_ids:
            raise ExperienceMemoryError("evidence_event_ids must be a non-empty list")
        payload = {
            "lesson_id": _required_text(lesson_id, "lesson_id"),
            "lesson": _required_text(lesson, "lesson"),
            "evidence_event_ids": [
                _required_text(item, "evidence_event_id") for item in evidence_event_ids
            ],
        }
        return self._append(
            experience_id=experience_id,
            event_id=event_id,
            event_type="LESSON_PROPOSED",
            payload=payload,
            timestamp_utc=timestamp_utc,
        )

    def validate_lesson(
        self,
        *,
        experience_id: str,
        event_id: str,
        lesson_id: str,
        independent_test: bool,
        test_dataset_id: str,
        sample_count: int,
        passed: bool,
        metrics: Mapping[str, Any],
        timestamp_utc: str | None = None,
    ) -> dict[str, Any]:
        """Append an independent validation result for a previously proposed lesson."""
        if independent_test is not True:
            raise ExperienceStateError(
                "lesson validation requires an independent test"
            )
        if isinstance(sample_count, bool) or not isinstance(sample_count, int) or sample_count < 1:
            raise ExperienceMemoryError("sample_count must be a positive integer")
        if not isinstance(passed, bool) or not isinstance(metrics, Mapping):
            raise ExperienceMemoryError("passed must be boolean and metrics must be an object")
        payload = {
            "lesson_id": _required_text(lesson_id, "lesson_id"),
            "independent_test": True,
            "test_dataset_id": _required_text(test_dataset_id, "test_dataset_id"),
            "sample_count": sample_count,
            "passed": passed,
            "validation_status": "VALIDATED" if passed else "REJECTED",
            "metrics": dict(metrics),
        }
        _reject_secrets(payload)
        return self._append(
            experience_id=experience_id,
            event_id=event_id,
            event_type="LESSON_VALIDATION_RECORDED",
            payload=payload,
            timestamp_utc=timestamp_utc,
        )

    def _append(
        self,
        *,
        experience_id: str,
        event_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        metadata: Mapping[str, str] | None = None,
        timestamp_utc: str | None = None,
    ) -> dict[str, Any]:
        experience_key = _required_text(experience_id, "experience_id")
        event_key = _required_text(event_id, "event_id")
        event_kind = _required_text(event_type, "event_type").upper()
        if event_kind not in EVENT_TYPES:
            raise ExperienceMemoryError(f"unsupported event_type: {event_kind}")
        if not isinstance(payload, Mapping):
            raise ExperienceMemoryError("payload must be an object")
        _reject_secrets(payload)
        payload_json = _canonical_json(dict(payload))
        payload_hash = _sha256(payload_json)
        timestamp = _timestamp_utc(timestamp_utc)

        with self._lock:
            duplicate = self._db.execute(
                "SELECT * FROM learning_experience_events WHERE event_id=?",
                (event_key,),
            ).fetchone()
            if duplicate is not None:
                saved = self._as_dict(duplicate)
                same = (
                    saved["experience_id"] == experience_key
                    and saved["event_type"] == event_kind
                    and saved["payload_json"] == payload_json
                    and saved["payload_hash"] == payload_hash
                )
                if metadata is not None:
                    same = same and all(saved[key] == value for key, value in metadata.items())
                if timestamp_utc is not None:
                    same = same and saved["timestamp_utc"] == timestamp
                if not same:
                    raise ExperienceIdempotencyConflict(
                        "event_id was already used for different immutable content"
                    )
                verification = self.verify_experience(experience_key)
                if verification["status"] != "PASS":
                    raise ExperienceIntegrityError("; ".join(verification["errors"]))
                saved["payload"] = json.loads(saved.pop("payload_json"))
                saved["inserted"] = False
                return saved

            rows = self._load_rows(experience_key)
            if not rows:
                if event_kind != "DECISION_RECORDED" or metadata is None:
                    raise ExperienceNotFoundError(experience_key)
                if set(metadata) != {
                    "engine_id", "challenge_id", "cycle_id",
                    "decision_mode", "environment",
                }:
                    raise ExperienceMemoryError("incomplete experience metadata")
                meta = dict(metadata)
                if meta["decision_mode"] not in DECISION_MODES or meta["environment"] not in ENVIRONMENTS:
                    raise ExperienceMemoryError("unsupported decision_mode or environment")
                sequence = 1
                previous_hash = None
            else:
                verification = self._verify_rows(rows)
                if verification["status"] != "PASS":
                    raise ExperienceIntegrityError("; ".join(verification["errors"]))
                meta = {key: rows[0][key] for key in (
                    "engine_id", "challenge_id", "cycle_id",
                    "decision_mode", "environment",
                )}
                if metadata is not None and dict(metadata) != meta:
                    raise ExperienceIdempotencyConflict(
                        "experience metadata cannot change after creation"
                    )
                self._validate_next_event(rows, event_kind, dict(payload))
                sequence = int(rows[-1]["experience_sequence"]) + 1
                previous_hash = rows[-1]["event_hash"]
                if timestamp < rows[-1]["timestamp_utc"]:
                    raise ExperienceStateError("experience event timestamp cannot move backwards")

            event = {
                "event_id": event_key,
                "experience_id": experience_key,
                "experience_sequence": sequence,
                **meta,
                "event_type": event_kind,
                "timestamp_utc": timestamp,
                "payload_json": payload_json,
                "payload_hash": payload_hash,
                "previous_event_hash": previous_hash,
            }
            event["event_hash"] = self._event_hash_payload(event)
            self._db.execute(
                """INSERT INTO learning_experience_events
                   (event_id, experience_id, experience_sequence, engine_id, challenge_id,
                    cycle_id, decision_mode, environment, event_type, timestamp_utc,
                    payload_json, payload_hash, previous_event_hash, event_hash)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event["event_id"], event["experience_id"], event["experience_sequence"],
                    event["engine_id"], event["challenge_id"], event["cycle_id"],
                    event["decision_mode"], event["environment"], event["event_type"],
                    event["timestamp_utc"], event["payload_json"], event["payload_hash"],
                    event["previous_event_hash"], event["event_hash"],
                ),
            )
            saved = self._db.execute(
                "SELECT * FROM learning_experience_events WHERE event_id=?", (event_key,)
            ).fetchone()
            result = self._as_dict(saved)
            result["payload"] = json.loads(result.pop("payload_json"))
            result["inserted"] = True
            return result

    @staticmethod
    def _validate_next_event(
        rows: list[dict[str, Any]], event_type: str, payload: dict[str, Any]
    ) -> None:
        root = rows[0]
        types = [row["event_type"] for row in rows]
        if event_type == "DECISION_RECORDED":
            raise ExperienceStateError("an experience can contain only one immutable decision")
        if event_type == "OUTCOME_LABELLED":
            if "OUTCOME_LABELLED" in types:
                raise ExperienceStateError(
                    "a final outcome is immutable and can be recorded only once"
                )
            if payload.get("final") is not True:
                raise ExperienceStateError("only a final outcome can be appended")
        elif event_type == "MENTOR_FEEDBACK_RECORDED":
            if root["decision_mode"] != "BOT_ONLY" or "OUTCOME_LABELLED" not in types:
                raise ExperienceStateError(
                    "mentor feedback requires a final-labelled BOT_ONLY decision"
                )
            if "MENTOR_FEEDBACK_RECORDED" in types:
                raise ExperienceStateError(
                    "mentor feedback for a baseline can be recorded only once"
                )
        elif event_type == "LESSON_PROPOSED":
            if "OUTCOME_LABELLED" not in types:
                raise ExperienceStateError("a lesson requires a final outcome")
            lesson_id = _required_text(payload.get("lesson_id"), "lesson_id")
            existing_ids = {
                json.loads(row["payload_json"]).get("lesson_id")
                for row in rows if row["event_type"] == "LESSON_PROPOSED"
            }
            if lesson_id in existing_ids:
                raise ExperienceStateError("lesson_id is already present in this experience")
            evidence_ids = payload.get("evidence_event_ids")
            known_event_ids = {row["event_id"] for row in rows}
            if (
                not isinstance(evidence_ids, list)
                or not evidence_ids
                or any(item not in known_event_ids for item in evidence_ids)
            ):
                raise ExperienceStateError(
                    "lesson evidence_event_ids must reference existing events in this experience"
                )
        elif event_type == "LESSON_VALIDATION_RECORDED":
            lesson_id = _required_text(payload.get("lesson_id"), "lesson_id")
            proposed_ids = {
                json.loads(row["payload_json"]).get("lesson_id")
                for row in rows if row["event_type"] == "LESSON_PROPOSED"
            }
            if lesson_id not in proposed_ids:
                raise ExperienceStateError(
                    "lesson validation requires a previously proposed lesson"
                )
            if payload.get("independent_test") is not True:
                raise ExperienceStateError(
                    "lesson validation requires independent_test=true"
                )
            dataset_id = _required_text(
                payload.get("test_dataset_id"), "test_dataset_id"
            )
            prior_datasets = {
                json.loads(row["payload_json"]).get("test_dataset_id")
                for row in rows
                if row["event_type"] == "LESSON_VALIDATION_RECORDED"
                and json.loads(row["payload_json"]).get("lesson_id") == lesson_id
            }
            if dataset_id in prior_datasets:
                raise ExperienceStateError(
                    "the same lesson cannot be validated twice on the same dataset"
                )
        else:
            raise ExperienceStateError(f"unsupported lifecycle event: {event_type}")

    def _load_rows(self, experience_id: str) -> list[dict[str, Any]]:
        rows = self._db.execute(
            """SELECT * FROM learning_experience_events
               WHERE experience_id=? ORDER BY experience_sequence ASC""",
            (experience_id,),
        ).fetchall()
        return [self._as_dict(row) for row in rows]

    def verify_experience(self, experience_id: str) -> dict[str, Any]:
        """Verify sequence, immutable metadata, payload hashes and event chain."""
        experience_key = _required_text(experience_id, "experience_id")
        with self._lock:
            rows = self._load_rows(experience_key)
            return self._verify_rows(rows, experience_key)

    def _verify_rows(
        self, rows: list[dict[str, Any]], experience_id: str | None = None
    ) -> dict[str, Any]:
        errors: list[str] = []
        if not rows:
            return {
                "status": "FAIL",
                "experience_id": experience_id,
                "event_count": 0,
                "errors": ["experience not found"],
            }

        root = rows[0]
        metadata_keys = (
            "engine_id", "challenge_id", "cycle_id",
            "decision_mode", "environment",
        )
        expected_meta = {key: root[key] for key in metadata_keys}
        if root["event_type"] != "DECISION_RECORDED" or int(root["experience_sequence"]) != 1:
            errors.append("first event must be DECISION_RECORDED at sequence 1")
        if root["decision_mode"] not in DECISION_MODES:
            errors.append("root decision_mode is invalid")
        if root["environment"] not in ENVIRONMENTS:
            errors.append("root environment is invalid")

        previous_hash: str | None = None
        previous_timestamp: str | None = None
        for expected_sequence, row in enumerate(rows, 1):
            if int(row["experience_sequence"]) != expected_sequence:
                errors.append(
                    f"sequence gap: expected {expected_sequence}, got {row['experience_sequence']}"
                )
            if any(row[key] != value for key, value in expected_meta.items()):
                errors.append(f"event {expected_sequence}: immutable metadata changed")
            try:
                payload = json.loads(row["payload_json"])
                if not isinstance(payload, dict):
                    raise ValueError("payload is not an object")
                _reject_secrets(payload)
                if _sha256(_canonical_json(payload)) != row["payload_hash"]:
                    errors.append(f"event {expected_sequence}: payload hash mismatch")
                if _timestamp_utc(row["timestamp_utc"]) != row["timestamp_utc"]:
                    errors.append(f"event {expected_sequence}: timestamp is not normalized UTC")
            except (json.JSONDecodeError, ExperienceMemoryError, TypeError, ValueError):
                payload = {}
                errors.append(f"event {expected_sequence}: invalid payload or timestamp")

            if previous_timestamp is not None and row["timestamp_utc"] < previous_timestamp:
                errors.append(f"event {expected_sequence}: timestamp moved backwards")
            previous_timestamp = row["timestamp_utc"]

            if row["previous_event_hash"] != previous_hash:
                errors.append(f"event {expected_sequence}: previous hash mismatch")
            try:
                expected_hash = self._event_hash_payload(row)
            except (KeyError, TypeError, ValueError):
                expected_hash = None
            if expected_hash != row["event_hash"]:
                errors.append(f"event {expected_sequence}: event hash mismatch")

            if expected_sequence > 1:
                try:
                    self._validate_next_event(
                        rows[:expected_sequence - 1], row["event_type"], payload
                    )
                except ExperienceMemoryError as exc:
                    errors.append(
                        f"event {expected_sequence}: invalid lifecycle: {exc}"
                    )
            previous_hash = str(row["event_hash"])

        try:
            root_payload = json.loads(root["payload_json"])
            for key, value in expected_meta.items():
                if root_payload.get(key) != value:
                    errors.append(f"root payload metadata mismatch: {key}")
        except (json.JSONDecodeError, TypeError):
            errors.append("root payload is invalid")

        return {
            "status": "PASS" if not errors else "FAIL",
            "experience_id": experience_id or root["experience_id"],
            "event_count": len(rows),
            "last_event_hash": previous_hash,
            "errors": errors,
        }

    def get_experience(self, experience_id: str) -> dict[str, Any]:
        """Return verified history and a derived learning status."""
        experience_key = _required_text(experience_id, "experience_id")
        with self._lock:
            rows = self._load_rows(experience_key)
            verification = self._verify_rows(rows, experience_key)
            if verification["status"] != "PASS":
                raise ExperienceIntegrityError("; ".join(verification["errors"]))
            events = []
            for row in rows:
                item = dict(row)
                item["payload"] = json.loads(item.pop("payload_json"))
                events.append(item)
            event_types = [item["event_type"] for item in events]
            validation_results = [
                item["payload"] for item in events
                if item["event_type"] == "LESSON_VALIDATION_RECORDED"
            ]
            if any(item.get("passed") is True for item in validation_results):
                status = "LESSON_VALIDATED"
            elif validation_results:
                status = "LESSON_TESTED_NOT_PASSED"
            elif "LESSON_PROPOSED" in event_types:
                status = "LESSON_PROPOSED"
            elif "MENTOR_FEEDBACK_RECORDED" in event_types:
                status = "MENTOR_REVIEWED"
            elif "OUTCOME_LABELLED" in event_types:
                status = "OUTCOME_RECORDED"
            else:
                status = "PENDING_OUTCOME"
            root = events[0]
            return {
                "experience_id": experience_key,
                "engine_id": root["engine_id"],
                "challenge_id": root["challenge_id"],
                "cycle_id": root["cycle_id"],
                "decision_mode": root["decision_mode"],
                "environment": root["environment"],
                "learning_status": status,
                "events": events,
                "verification": verification,
            }

    def recent_experiences(
        self, limit: int = 50, *, challenge_id: str | None = None
    ) -> list[dict[str, Any]]:
        """List recent verified experiences; corrupt histories raise an error."""
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValueError("limit must be an integer")
        bounded = max(1, min(limit, 2000))
        if challenge_id is None:
            rows = self._db.execute(
                """SELECT experience_id FROM learning_experience_events
                   WHERE experience_sequence=1 ORDER BY sequence DESC LIMIT ?""",
                (bounded,),
            ).fetchall()
        else:
            challenge = _required_text(challenge_id, "challenge_id")
            rows = self._db.execute(
                """SELECT experience_id FROM learning_experience_events
                   WHERE experience_sequence=1 AND challenge_id=?
                   ORDER BY sequence DESC LIMIT ?""",
                (challenge, bounded),
            ).fetchall()
        return [self.get_experience(row["experience_id"]) for row in rows]


__all__ = [
    "LearningExperienceMemory",
    "ExperienceMemoryError",
    "ExperienceIdempotencyConflict",
    "ExperienceIntegrityError",
    "ExperienceStateError",
    "ExperienceNotFoundError",
]
