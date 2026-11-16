"""SQLite connection management, schema, and low-level persistence methods."""
from __future__ import annotations

import json
import math
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from collections.abc import Mapping
from typing import Any, Iterator

from .event_store_common import _canonical_json, _sha256
from .order_state_machine import TransitionValidator


class EventStoreStorage:
    """SQLite schema and transaction/session primitives."""

    def __init__(self, db_path: str | Path, *, timeout_seconds: float = 5.0):
        path_value = str(db_path)
        if not path_value.strip():
            raise ValueError("db_path is required")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise ValueError("timeout_seconds must be positive")
        if not math.isfinite(float(timeout_seconds)) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

        self.db_path = path_value
        self.timeout_seconds = float(timeout_seconds)
        self._is_memory = path_value == ":memory:"
        self._lock = threading.RLock()
        self._closed = False
        self._memory_connection: sqlite3.Connection | None = None
        self._validator = TransitionValidator()

        if self._is_memory:
            self._memory_connection = self._new_connection()
            self._initialize(self._memory_connection)
        else:
            path = Path(path_value).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            self.db_path = str(path)
            connection = self._new_connection()
            try:
                # WAL is a database-level initialization choice, not a per-operation pragma.
                connection.execute("PRAGMA journal_mode=WAL").fetchone()
                self._initialize(connection)
            finally:
                connection.close()

    def _new_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.db_path, timeout=self.timeout_seconds, isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={int(self.timeout_seconds * 1000)}")
        return connection

    @staticmethod
    def _initialize(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS trade_intents (
                intent_id TEXT PRIMARY KEY,
                client_order_id TEXT NOT NULL UNIQUE,
                payload_json TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                current_state TEXT NOT NULL,
                created_at_utc TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS trade_events (
                event_id TEXT PRIMARY KEY,
                intent_id TEXT NOT NULL REFERENCES trade_intents(intent_id),
                seq_num INTEGER NOT NULL CHECK(seq_num >= 1),
                client_order_id TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                from_state TEXT NOT NULL,
                to_state TEXT NOT NULL,
                event_type TEXT NOT NULL,
                exchange_order_id TEXT,
                evidence_json TEXT NOT NULL,
                timestamp_utc TEXT NOT NULL,
                previous_event_hash TEXT,
                event_hash TEXT NOT NULL,
                UNIQUE(intent_id, seq_num)
            );

            CREATE INDEX IF NOT EXISTS idx_trade_events_intent_sequence
                ON trade_events(intent_id, seq_num);
            CREATE INDEX IF NOT EXISTS idx_trade_events_client_order
                ON trade_events(client_order_id, seq_num);

            CREATE TRIGGER IF NOT EXISTS trade_events_no_update
            BEFORE UPDATE ON trade_events
            BEGIN SELECT RAISE(ABORT, 'trade events are append-only'); END;

            CREATE TRIGGER IF NOT EXISTS trade_events_no_delete
            BEFORE DELETE ON trade_events
            BEGIN SELECT RAISE(ABORT, 'trade events are append-only'); END;

            CREATE TRIGGER IF NOT EXISTS trade_intents_identity_immutable
            BEFORE UPDATE ON trade_intents
            WHEN OLD.intent_id != NEW.intent_id
              OR OLD.client_order_id != NEW.client_order_id
              OR OLD.payload_json != NEW.payload_json
              OR OLD.payload_hash != NEW.payload_hash
              OR OLD.created_at_utc != NEW.created_at_utc
            BEGIN SELECT RAISE(ABORT, 'trade intent identity is immutable'); END;
            """
        )

    @contextmanager
    def _session(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        if self._closed:
            raise RuntimeError("PersistentEventStore is closed")
        if self._is_memory:
            with self._lock:
                connection = self._memory_connection
                if connection is None:
                    raise RuntimeError("in-memory database is closed")
                if write:
                    connection.execute("BEGIN IMMEDIATE")
                try:
                    yield connection
                    if write:
                        connection.commit()
                except Exception:
                    if write and connection.in_transaction:
                        connection.rollback()
                    raise
            return

        connection = self._new_connection()
        try:
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            if write:
                connection.commit()
        except Exception:
            if write and connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _intent_dict(row: sqlite3.Row, *, created: bool | None = None) -> dict[str, Any]:
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        if created is not None:
            result["created"] = created
        return result

    @staticmethod
    def _event_dict(row: sqlite3.Row, *, inserted: bool | None = None) -> dict[str, Any]:
        result = dict(row)
        result["evidence"] = json.loads(result.pop("evidence_json"))
        if inserted is not None:
            result["inserted"] = inserted
        return result

    @staticmethod
    def _event_hash_payload(event: Mapping[str, Any]) -> str:
        fields = {
            "event_id": event["event_id"],
            "intent_id": event["intent_id"],
            "seq_num": int(event["seq_num"]),
            "client_order_id": event["client_order_id"],
            "payload_hash": event["payload_hash"],
            "from_state": event["from_state"],
            "to_state": event["to_state"],
            "event_type": event["event_type"],
            "exchange_order_id": event["exchange_order_id"],
            "evidence_json": event["evidence_json"],
            "timestamp_utc": event["timestamp_utc"],
            "previous_event_hash": event["previous_event_hash"],
        }
        return _sha256(_canonical_json(fields))

    @staticmethod
    def _insert_event(connection: sqlite3.Connection, event: Mapping[str, Any]) -> None:
        connection.execute(
            """INSERT INTO trade_events
            (event_id, intent_id, seq_num, client_order_id, payload_hash, from_state, to_state,
             event_type, exchange_order_id, evidence_json, timestamp_utc, previous_event_hash, event_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event["event_id"], event["intent_id"], event["seq_num"], event["client_order_id"],
                event["payload_hash"], event["from_state"], event["to_state"], event["event_type"],
                event["exchange_order_id"], event["evidence_json"], event["timestamp_utc"],
                event["previous_event_hash"], event["event_hash"],
            ),
        )
