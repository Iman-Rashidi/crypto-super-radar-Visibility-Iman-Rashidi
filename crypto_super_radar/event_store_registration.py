"""Write-side atomic intent registration."""
from __future__ import annotations
from collections.abc import Mapping
from typing import Any
from .event_store_common import IdempotencyViolationError, EventChainIntegrityError, _canonical_json, _sha256, _utc_timestamp, _required_text


class EventStoreRegistration:
    """Immutable intent registration and idempotent replay."""

    def register_or_fetch_intent(
            self,
            *,
            intent_id: str,
            client_order_id: str,
            payload: Mapping[str, Any],
            payload_hash: str | None = None,
            timestamp_utc: str | None = None,
        ) -> dict[str, Any]:
            """Atomically register an immutable intent and its initial event.

            Exact replay returns the prior intent. Reusing either identity with a
            different client-order ID or payload hash raises an idempotency error.
            """
            intent_key = _required_text(intent_id, "intent_id")
            client_key = _required_text(client_order_id, "client_order_id")
            if not isinstance(payload, Mapping):
                raise ValueError("payload must be a mapping")
            payload_json = _canonical_json(dict(payload))
            computed_hash = _sha256(payload_json)
            if payload_hash is not None and payload_hash != computed_hash:
                raise IdempotencyViolationError("provided payload_hash does not match canonical payload")
            hash_value = computed_hash
            timestamp = _utc_timestamp(timestamp_utc)

            with self._session(write=True) as connection:
                existing = connection.execute(
                    "SELECT * FROM trade_intents WHERE intent_id=?", (intent_key,)
                ).fetchone()
                if existing is not None:
                    if existing["client_order_id"] != client_key or existing["payload_hash"] != hash_value:
                        raise IdempotencyViolationError(
                            "existing intent_id belongs to a different client_order_id or payload_hash"
                        )
                    integrity = self._verify_chain(connection, intent_key)
                    if integrity["status"] != "PASS":
                        raise EventChainIntegrityError("existing intent event chain is invalid: " + "; ".join(integrity["errors"]))
                    return self._intent_dict(existing, created=False)

                collision = connection.execute(
                    "SELECT intent_id, payload_hash FROM trade_intents WHERE client_order_id=?",
                    (client_key,),
                ).fetchone()
                if collision is not None:
                    raise IdempotencyViolationError("client_order_id is already assigned to another intent_id")

                connection.execute(
                    """INSERT INTO trade_intents
                    (intent_id, client_order_id, payload_json, payload_hash, current_state, created_at_utc, updated_at_utc)
                    VALUES (?, ?, ?, ?, 'INTENT_RECORDED', ?, ?)""",
                    (intent_key, client_key, payload_json, hash_value, timestamp, timestamp),
                )
                initial = {
                    "event_id": f"intent:{intent_key}:registered",
                    "intent_id": intent_key,
                    "seq_num": 1,
                    "client_order_id": client_key,
                    "payload_hash": hash_value,
                    "from_state": "NONE",
                    "to_state": "INTENT_RECORDED",
                    "event_type": "INTENT_REGISTERED",
                    "exchange_order_id": None,
                    "evidence_json": "{}",
                    "timestamp_utc": timestamp,
                    "previous_event_hash": None,
                }
                initial["event_hash"] = self._event_hash_payload(initial)
                self._insert_event(connection, initial)
                row = connection.execute(
                    "SELECT * FROM trade_intents WHERE intent_id=?", (intent_key,)
                ).fetchone()
                return self._intent_dict(row, created=True)
