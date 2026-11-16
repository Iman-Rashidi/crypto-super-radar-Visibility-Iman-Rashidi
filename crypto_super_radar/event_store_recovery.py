"""Read-side recovery, event-chain validation, and resource cleanup."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from .event_store_common import (
    EventChainIntegrityError, _canonical_json, _sha256, _utc_timestamp, _required_text,
)
from .order_state_machine import TransitionValidationError


class EventStoreRecovery:
    """Replay the append-only event journal and validate its projection."""

    def get_intent(self, intent_id: str, *, verify_chain: bool = True) -> dict[str, Any] | None:
        intent_key = _required_text(intent_id, "intent_id")
        with self._session() as connection:
            row = connection.execute(
                "SELECT * FROM trade_intents WHERE intent_id=?", (intent_key,)
            ).fetchone()
            if row is None:
                return None
            if verify_chain:
                result = self._verify_chain(connection, intent_key)
                if result["status"] != "PASS":
                    raise EventChainIntegrityError("intent recovery failed: " + "; ".join(result["errors"]))
            return self._intent_dict(row)

    def list_events(self, intent_id: str) -> list[dict[str, Any]]:
        intent_key = _required_text(intent_id, "intent_id")
        with self._session() as connection:
            rows = connection.execute(
                "SELECT * FROM trade_events WHERE intent_id=? ORDER BY seq_num ASC", (intent_key,)
            ).fetchall()
            return [self._event_dict(row) for row in rows]

    def verify_event_chain(self, intent_id: str) -> dict[str, Any]:
        intent_key = _required_text(intent_id, "intent_id")
        with self._session() as connection:
            return self._verify_chain(connection, intent_key)

    def _verify_chain(self, connection: sqlite3.Connection, intent_id: str) -> dict[str, Any]:
        intent = connection.execute(
            "SELECT * FROM trade_intents WHERE intent_id=?", (intent_id,)
        ).fetchone()
        if intent is None:
            return {"status": "FAIL", "intent_id": intent_id, "errors": ["intent not found"], "event_count": 0}
        try:
            payload = json.loads(intent["payload_json"])
            recomputed_payload_hash = _sha256(_canonical_json(payload))
        except (json.JSONDecodeError, TypeError, ValueError):
            recomputed_payload_hash = None
        errors: list[str] = []
        if recomputed_payload_hash != intent["payload_hash"]:
            errors.append("intent payload hash does not match stored payload")

        rows = connection.execute(
            "SELECT * FROM trade_events WHERE intent_id=? ORDER BY seq_num ASC", (intent_id,)
        ).fetchall()
        if not rows:
            errors.append("intent has no events")
        previous_state = "NONE"
        previous_hash: str | None = None
        previous_sequence = 0
        previous_timestamp: str | None = None
        previous_exchange_id: str | None = None
        for row in rows:
            event = dict(row)
            seq = int(event["seq_num"])
            if seq != previous_sequence + 1:
                errors.append(f"sequence gap: expected {previous_sequence + 1}, got {seq}")
            if event["client_order_id"] != intent["client_order_id"]:
                errors.append(f"event {seq}: client_order_id mismatch")
            if event["payload_hash"] != intent["payload_hash"]:
                errors.append(f"event {seq}: payload_hash mismatch")
            if event["from_state"] != previous_state:
                errors.append(f"event {seq}: from_state does not match previous to_state")
            evidence: dict[str, Any]
            try:
                evidence = json.loads(event["evidence_json"])
                if not isinstance(evidence, dict):
                    raise ValueError("evidence is not an object")
                self._validator.validate(event["from_state"], event["to_state"], evidence)
                if event["from_state"] == "UNKNOWN":
                    if event["event_type"] != "RECONCILIATION":
                        raise ValueError("UNKNOWN transition event_type must be RECONCILIATION")
                    if event["to_state"] in {"ACKNOWLEDGED", "PARTIALLY_FILLED", "FILLED", "POSITION_OPEN_UNPROTECTED"}:
                        if evidence.get("exchange_order_id") != event["exchange_order_id"]:
                            raise ValueError("reconciliation evidence exchange_order_id does not match event")
                if event["from_state"] == "POSITION_OPEN_UNPROTECTED" and event["to_state"] == "POSITION_MANAGED" and event["event_type"] != "PROTECTION_VERIFIED":
                    raise ValueError("protection transition event_type must be PROTECTION_VERIFIED")
            except (json.JSONDecodeError, ValueError, TransitionValidationError) as exc:
                errors.append(f"event {seq}: invalid transition evidence: {exc}")
            if previous_exchange_id is not None and event["exchange_order_id"] != previous_exchange_id:
                errors.append(f"event {seq}: exchange_order_id changed or disappeared")
            if previous_exchange_id is None and event["exchange_order_id"] is not None:
                previous_exchange_id = event["exchange_order_id"]
            if event["to_state"] in {"ACKNOWLEDGED", "PARTIALLY_FILLED", "FILLED"} and event["exchange_order_id"] is None:
                errors.append(f"event {seq}: {event['to_state']} is missing exchange_order_id")
            try:
                normalized_timestamp = _utc_timestamp(event["timestamp_utc"])
                if normalized_timestamp != event["timestamp_utc"]:
                    errors.append(f"event {seq}: timestamp is not normalized UTC milliseconds")
                if previous_timestamp is not None and normalized_timestamp < previous_timestamp:
                    errors.append(f"event {seq}: timestamp moved backwards")
                previous_timestamp = normalized_timestamp
            except (TypeError, ValueError):
                errors.append(f"event {seq}: invalid UTC timestamp")
            if event["previous_event_hash"] != previous_hash:
                errors.append(f"event {seq}: previous event hash mismatch")
            try:
                expected_hash = self._event_hash_payload(event)
            except (KeyError, TypeError, ValueError):
                expected_hash = None
            if expected_hash != event["event_hash"]:
                errors.append(f"event {seq}: event hash mismatch")
            if seq == 1 and (
                event["from_state"] != "NONE"
                or event["to_state"] != "INTENT_RECORDED"
                or event["event_type"] != "INTENT_REGISTERED"
            ):
                errors.append("first event is not the expected initial intent event")
            previous_state = str(event["to_state"])
            previous_hash = str(event["event_hash"])
            previous_sequence = seq

        try:
            created_at = _utc_timestamp(intent["created_at_utc"])
            updated_at = _utc_timestamp(intent["updated_at_utc"])
            if created_at != intent["created_at_utc"] or updated_at != intent["updated_at_utc"]:
                errors.append("intent timestamps are not normalized UTC milliseconds")
            if rows and updated_at != rows[-1]["timestamp_utc"]:
                errors.append("intent updated_at_utc does not match last event timestamp")
        except (TypeError, ValueError):
            errors.append("intent has invalid UTC timestamps")
        if rows and previous_state != intent["current_state"]:
            errors.append("last event state does not match trade_intents.current_state")
        if not rows and intent["current_state"] != "INTENT_RECORDED":
            errors.append("intent current_state cannot be recovered without an event")
        return {
            "status": "PASS" if not errors else "FAIL",
            "intent_id": intent_id,
            "event_count": len(rows),
            "last_state": previous_state if rows else None,
            "last_event_hash": previous_hash,
            "errors": errors,
        }

    def close(self) -> None:
        """Close the retained in-memory connection; file-backed operations are per-call."""
        with self._lock:
            if self._closed:
                return
            if self._memory_connection is not None:
                self._memory_connection.close()
                self._memory_connection = None
            self._closed = True
