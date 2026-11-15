"""Write-side atomic lifecycle transitions."""
from __future__ import annotations
from collections.abc import Mapping
from typing import Any
from .event_store_common import IdempotencyViolationError, EventChainIntegrityError, ConcurrentStateChangeError, _canonical_json, _utc_timestamp, _required_text
from .order_state_machine import TransitionValidationError


class EventStoreTransitions:
    """Append-only, evidence-validated order and position transitions."""

    def commit_transition_atomic(
            self,
            *,
            intent_id: str,
            client_order_id: str,
            payload_hash: str,
            to_state: str,
            event_type: str,
            event_id: str,
            exchange_order_id: str | None = None,
            evidence: Mapping[str, Any] | None = None,
            timestamp_utc: str | None = None,
        ) -> dict[str, Any]:
            """Append one transition and update the intent projection atomically.

            'event_id' is caller-supplied and must be stable across retries. An
            identical replay returns the original event; conflicting reuse fails.
            """
            intent_key = _required_text(intent_id, "intent_id")
            client_key = _required_text(client_order_id, "client_order_id")
            hash_value = _required_text(payload_hash, "payload_hash")
            target_state = _required_text(to_state, "to_state").upper()
            event_kind = _required_text(event_type, "event_type").upper()
            event_key = _required_text(event_id, "event_id")
            exchange_id = None if exchange_order_id is None else _required_text(exchange_order_id, "exchange_order_id")
            if evidence is None:
                evidence = {}
            if not isinstance(evidence, Mapping):
                raise ValueError("evidence must be a mapping")
            evidence_json = _canonical_json(dict(evidence))
            timestamp = _utc_timestamp(timestamp_utc)

            with self._session(write=True) as connection:
                duplicate = connection.execute(
                    "SELECT * FROM trade_events WHERE event_id=?", (event_key,)
                ).fetchone()
                if duplicate is not None:
                    same = (
                        duplicate["intent_id"] == intent_key
                        and duplicate["client_order_id"] == client_key
                        and duplicate["payload_hash"] == hash_value
                        and duplicate["to_state"] == target_state
                        and duplicate["event_type"] == event_kind
                        and duplicate["exchange_order_id"] == exchange_id
                        and duplicate["evidence_json"] == evidence_json
                        and (timestamp_utc is None or duplicate["timestamp_utc"] == timestamp)
                    )
                    if not same:
                        raise IdempotencyViolationError("event_id was already used for different event content")
                    integrity = self._verify_chain(connection, intent_key)
                    if integrity["status"] != "PASS":
                        raise EventChainIntegrityError("duplicate event belongs to an invalid chain")
                    return self._event_dict(duplicate, inserted=False)

                intent = connection.execute(
                    "SELECT * FROM trade_intents WHERE intent_id=?", (intent_key,)
                ).fetchone()
                if intent is None:
                    raise KeyError(f"intent_id not found: {intent_key}")
                if intent["client_order_id"] != client_key or intent["payload_hash"] != hash_value:
                    raise IdempotencyViolationError("client_order_id or payload_hash does not match the registered intent")

                integrity = self._verify_chain(connection, intent_key)
                if integrity["status"] != "PASS":
                    raise EventChainIntegrityError("cannot append to an invalid event chain: " + "; ".join(integrity["errors"]))
                previous = connection.execute(
                    "SELECT * FROM trade_events WHERE intent_id=? ORDER BY seq_num DESC LIMIT 1",
                    (intent_key,),
                ).fetchone()
                if previous is None:
                    raise EventChainIntegrityError("intent has no initial event")
                from_state = str(intent["current_state"])
                self._validator.validate(from_state, target_state, evidence)
                previous_exchange_id = previous["exchange_order_id"]
                if previous_exchange_id is not None:
                    if exchange_id is not None and exchange_id != previous_exchange_id:
                        raise IdempotencyViolationError("exchange_order_id changed during the intent lifecycle")
                    exchange_id = previous_exchange_id
                elif target_state in {"ACKNOWLEDGED", "PARTIALLY_FILLED", "FILLED"} and exchange_id is None:
                    raise TransitionValidationError(f"{target_state} requires a verified exchange_order_id")
                if from_state == "UNKNOWN" and target_state in {"ACKNOWLEDGED", "PARTIALLY_FILLED", "FILLED", "POSITION_OPEN_UNPROTECTED"}:
                    if exchange_id is None or evidence.get("exchange_order_id") != exchange_id:
                        raise TransitionValidationError("reconciliation evidence must identify the matching exchange_order_id")
                if from_state == "UNKNOWN" and event_kind != "RECONCILIATION":
                    raise TransitionValidationError("UNKNOWN transitions must use event_type=RECONCILIATION")
                if from_state == "POSITION_OPEN_UNPROTECTED" and target_state == "POSITION_MANAGED" and event_kind != "PROTECTION_VERIFIED":
                    raise TransitionValidationError("POSITION_MANAGED requires event_type=PROTECTION_VERIFIED")
                sequence = int(previous["seq_num"]) + 1
                event = {
                    "event_id": event_key,
                    "intent_id": intent_key,
                    "seq_num": sequence,
                    "client_order_id": client_key,
                    "payload_hash": hash_value,
                    "from_state": from_state,
                    "to_state": target_state,
                    "event_type": event_kind,
                    "exchange_order_id": exchange_id,
                    "evidence_json": evidence_json,
                    "timestamp_utc": timestamp,
                    "previous_event_hash": previous["event_hash"],
                }
                event["event_hash"] = self._event_hash_payload(event)
                self._insert_event(connection, event)

                updated = connection.execute(
                    """UPDATE trade_intents
                    SET current_state=?, updated_at_utc=?
                    WHERE intent_id=? AND current_state=? AND client_order_id=? AND payload_hash=?""",
                    (target_state, timestamp, intent_key, from_state, client_key, hash_value),
                )
                if updated.rowcount != 1:
                    raise ConcurrentStateChangeError("intent state changed before atomic update completed")
                saved = connection.execute(
                    "SELECT * FROM trade_events WHERE event_id=?", (event_key,)
                ).fetchone()
                return self._event_dict(saved, inserted=True)
