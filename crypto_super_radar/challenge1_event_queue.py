"""Durable one-way evidence queue between Challenge 1 Sandbox and BloFin Demo.

This queue transports audit/learning evidence only. It deliberately exposes no
exchange client, order command, execution callback, or trading capability.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from collections.abc import Mapping
from typing import Any

from .database import connect_database, storage_backend


class Challenge1QueueError(RuntimeError):
    pass


class QueueIdempotencyConflict(Challenge1QueueError):
    pass


_ALLOWED_ROUTES = {
    "AI_SANDBOX": {
        "consumer": "DEMO_AUDIT",
        "event_types": {
            "REPLAY_REPORT",
            "REPLAY_BLOCKED",
            "SHADOW_BOOK_DELTA",
            "MODEL_CALIBRATION_REPORT",
        },
    },
    "BLOFIN_DEMO_ENGINE": {
        "consumer": "SANDBOX",
        "event_types": {
            "DEMO_EXECUTION_EVIDENCE",
            "DEMO_POSITION_OUTCOME",
            "DEMO_RECONCILIATION",
        },
    },
}
_FORBIDDEN_PAYLOAD_KEYS = {
    "action",
    "command",
    "execute",
    "endpoint",
    "api_key",
    "secret",
    "passphrase",
    "close_all",
}
_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._-]{5,179}$")
_WORKER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._-]{1,79}$")
_MAX_PAYLOAD_BYTES = 64 * 1024


def _has_forbidden_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).strip().lower() in _FORBIDDEN_PAYLOAD_KEYS or _has_forbidden_key(child):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_has_forbidden_key(item) for item in value)
    return False


def _canonical_json(value: Mapping[str, Any]) -> str:
    try:
        result = json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise Challenge1QueueError("event payload is not strict JSON") from exc
    if len(result.encode("utf-8")) > _MAX_PAYLOAD_BYTES:
        raise Challenge1QueueError("event payload exceeds 64 KiB")
    return result


def _validate_event(event: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(event, Mapping):
        raise Challenge1QueueError("event must be a mapping")

    event_id = str(event.get("event_id") or "").strip()
    producer = str(event.get("producer") or "").strip().upper()
    event_type = str(event.get("event_type") or "").strip().upper()
    occurred_at = event.get("occurred_at_ms")
    payload = event.get("payload")

    if not _EVENT_ID_RE.fullmatch(event_id):
        raise Challenge1QueueError("event_id must be a stable 6-180 character identifier")
    route = _ALLOWED_ROUTES.get(producer)
    if route is None or event_type not in route["event_types"]:
        raise Challenge1QueueError("producer/event_type pair is not allowed")
    if isinstance(occurred_at, bool) or not isinstance(occurred_at, int) or occurred_at <= 0:
        raise Challenge1QueueError("occurred_at_ms must be a positive integer")
    if not isinstance(payload, Mapping):
        raise Challenge1QueueError("payload must be a mapping")

    payload_dict = dict(payload)
    if _has_forbidden_key(payload_dict):
        raise Challenge1QueueError("payload contains a prohibited command or credential field")
    payload_json = _canonical_json(payload_dict)

    return {
        "event_id": event_id,
        "producer": producer,
        "consumer": route["consumer"],
        "event_type": event_type,
        "occurred_at_ms": occurred_at,
        "payload_json": payload_json,
    }


class Challenge1EventQueue:
    """SQLite/Turso-compatible durable queue with leases and immutable history."""

    def __init__(self, connection: Any | None = None) -> None:
        self._owns_db = connection is None
        self.backend = storage_backend() if connection is None else "INJECTED_CONNECTION"
        self._db = connection or connect_database()
        self._lock = threading.RLock()
        ddl = (
            """CREATE TABLE IF NOT EXISTS csr_challenge1_queue_events (
                event_id TEXT PRIMARY KEY, producer TEXT NOT NULL, consumer TEXT NOT NULL,
                event_type TEXT NOT NULL, occurred_at_ms INTEGER NOT NULL,
                payload_json TEXT NOT NULL, created_at_ms INTEGER NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS csr_challenge1_queue_delivery (
                event_id TEXT NOT NULL, consumer TEXT NOT NULL, status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, available_at_ms INTEGER NOT NULL,
                lease_owner TEXT, lease_until_ms INTEGER, delivered_at_ms INTEGER,
                last_error_type TEXT, updated_at_ms INTEGER NOT NULL,
                PRIMARY KEY(event_id, consumer),
                FOREIGN KEY(event_id) REFERENCES csr_challenge1_queue_events(event_id)
            )""",
            """CREATE INDEX IF NOT EXISTS idx_csr_ch1_queue_claim
               ON csr_challenge1_queue_delivery(consumer, status, available_at_ms, lease_until_ms, updated_at_ms)""",
            """CREATE TABLE IF NOT EXISTS csr_challenge1_queue_history (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL,
                consumer TEXT NOT NULL, state TEXT NOT NULL, worker_id TEXT,
                occurred_at_ms INTEGER NOT NULL, details_json TEXT NOT NULL
            )""",
            """CREATE INDEX IF NOT EXISTS idx_csr_ch1_queue_history_event
               ON csr_challenge1_queue_history(event_id, sequence)""",
            """CREATE TRIGGER IF NOT EXISTS csr_ch1_queue_events_no_update
               BEFORE UPDATE ON csr_challenge1_queue_events
               BEGIN SELECT RAISE(ABORT, 'Challenge 1 queue events are append-only'); END""",
            """CREATE TRIGGER IF NOT EXISTS csr_ch1_queue_events_no_delete
               BEFORE DELETE ON csr_challenge1_queue_events
               BEGIN SELECT RAISE(ABORT, 'Challenge 1 queue events are append-only'); END""",
            """CREATE TRIGGER IF NOT EXISTS csr_ch1_queue_history_no_update
               BEFORE UPDATE ON csr_challenge1_queue_history
               BEGIN SELECT RAISE(ABORT, 'Challenge 1 queue history is append-only'); END""",
            """CREATE TRIGGER IF NOT EXISTS csr_ch1_queue_history_no_delete
               BEFORE DELETE ON csr_challenge1_queue_history
               BEGIN SELECT RAISE(ABORT, 'Challenge 1 queue history is append-only'); END""",
        )
        with self._db:
            for statement in ddl:
                self._db.execute(statement)

    def close(self) -> None:
        if self._owns_db:
            self._db.close()

    def _history(
        self,
        event_id: str,
        consumer: str,
        state: str,
        worker_id: str | None,
        now_ms: int,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self._db.execute(
            """INSERT INTO csr_challenge1_queue_history
               (event_id, consumer, state, worker_id, occurred_at_ms, details_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                event_id,
                consumer,
                state,
                worker_id,
                now_ms,
                _canonical_json(details or {}),
            ),
        )

    def enqueue(self, event: Mapping[str, Any], *, now_ms: int | None = None) -> bool:
        """Append one evidence event and its consumer delivery exactly once."""
        item = _validate_event(event)
        now = int(now_ms if now_ms is not None else time.time() * 1000)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                existing = self._db.execute(
                    "SELECT producer, consumer, event_type, occurred_at_ms, payload_json "
                    "FROM csr_challenge1_queue_events WHERE event_id=?",
                    (item["event_id"],),
                ).fetchone()
                if existing is not None:
                    existing_values = (
                        existing["producer"], existing["consumer"], existing["event_type"],
                        int(existing["occurred_at_ms"]), existing["payload_json"],
                    )
                    new_values = (
                        item["producer"], item["consumer"], item["event_type"],
                        int(item["occurred_at_ms"]), item["payload_json"],
                    )
                    if existing_values != new_values:
                        raise QueueIdempotencyConflict(
                            "event_id was already used for different immutable content"
                        )
                    delivery = self._db.execute(
                        "SELECT 1 FROM csr_challenge1_queue_delivery WHERE event_id=? AND consumer=?",
                        (item["event_id"], item["consumer"]),
                    ).fetchone()
                    if delivery is None:
                        raise Challenge1QueueError("event exists without its delivery row")
                    self._db.rollback()
                    return False

                self._db.execute(
                    """INSERT INTO csr_challenge1_queue_events
                       (event_id, producer, consumer, event_type, occurred_at_ms, payload_json, created_at_ms)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        item["event_id"], item["producer"], item["consumer"],
                        item["event_type"], item["occurred_at_ms"],
                        item["payload_json"], now,
                    ),
                )
                self._db.execute(
                    """INSERT INTO csr_challenge1_queue_delivery
                       (event_id, consumer, status, attempts, available_at_ms, updated_at_ms)
                       VALUES (?, ?, 'PENDING', 0, ?, ?)""",
                    (item["event_id"], item["consumer"], now, now),
                )
                self._history(
                    item["event_id"], item["consumer"], "PENDING", None, now,
                    {"producer": item["producer"], "event_type": item["event_type"]},
                )
                self._db.commit()
                return True
            except Exception:
                self._db.rollback()
                raise

    def claim_next(
        self,
        consumer: str,
        *,
        worker_id: str,
        lease_ms: int = 30_000,
        now_ms: int | None = None,
    ) -> dict[str, Any] | None:
        """Atomically lease the oldest pending/retry/expired delivery."""
        consumer_key = str(consumer or "").strip().upper()
        if consumer_key not in {"DEMO_AUDIT", "SANDBOX"}:
            raise Challenge1QueueError("consumer is not allowed")
        worker = str(worker_id or "").strip()
        if not _WORKER_RE.fullmatch(worker):
            raise Challenge1QueueError("worker_id is invalid")
        if isinstance(lease_ms, bool) or not isinstance(lease_ms, int) or lease_ms < 1000:
            raise Challenge1QueueError("lease_ms must be at least 1000")
        now = int(now_ms if now_ms is not None else time.time() * 1000)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute(
                    """SELECT d.event_id, d.attempts, e.producer, e.event_type,
                              e.occurred_at_ms, e.payload_json
                       FROM csr_challenge1_queue_delivery d
                       JOIN csr_challenge1_queue_events e ON e.event_id=d.event_id
                       WHERE d.consumer=?
                         AND d.available_at_ms<=?
                         AND (
                           d.status IN ('PENDING','RETRY')
                           OR (d.status='PROCESSING' AND d.lease_until_ms<=?)
                         )
                       ORDER BY d.updated_at_ms ASC, d.event_id ASC
                       LIMIT 1""",
                    (consumer_key, now, now),
                ).fetchone()
                if row is None:
                    self._db.commit()
                    return None
                lease_until = now + lease_ms
                attempts = int(row["attempts"]) + 1
                self._db.execute(
                    """UPDATE csr_challenge1_queue_delivery
                       SET status='PROCESSING', attempts=?, lease_owner=?,
                           lease_until_ms=?, delivered_at_ms=NULL,
                           last_error_type=NULL, updated_at_ms=?
                       WHERE event_id=? AND consumer=?""",
                    (attempts, worker, lease_until, now, row["event_id"], consumer_key),
                )
                self._history(
                    str(row["event_id"]), consumer_key, "PROCESSING", worker, now,
                    {"attempt": attempts, "lease_until_ms": lease_until},
                )
                self._db.commit()
                return {
                    "event_id": str(row["event_id"]),
                    "producer": str(row["producer"]),
                    "consumer": consumer_key,
                    "event_type": str(row["event_type"]),
                    "occurred_at_ms": int(row["occurred_at_ms"]),
                    "payload": json.loads(str(row["payload_json"])),
                    "attempt": attempts,
                    "lease_owner": worker,
                    "lease_until_ms": lease_until,
                }
            except Exception:
                self._db.rollback()
                raise

    def acknowledge(
        self,
        event_id: str,
        consumer: str,
        *,
        worker_id: str,
        now_ms: int | None = None,
    ) -> bool:
        """Mark one currently leased delivery complete; never changes source event."""
        event_key = str(event_id or "").strip()
        consumer_key = str(consumer or "").strip().upper()
        worker = str(worker_id or "").strip()
        now = int(now_ms if now_ms is not None else time.time() * 1000)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute(
                    """SELECT status, lease_owner FROM csr_challenge1_queue_delivery
                       WHERE event_id=? AND consumer=?""",
                    (event_key, consumer_key),
                ).fetchone()
                if row is None or row["status"] != "PROCESSING" or row["lease_owner"] != worker:
                    self._db.rollback()
                    return False
                self._db.execute(
                    """UPDATE csr_challenge1_queue_delivery
                       SET status='ACKNOWLEDGED', lease_owner=NULL, lease_until_ms=NULL,
                           delivered_at_ms=?, updated_at_ms=?
                       WHERE event_id=? AND consumer=?""",
                    (now, now, event_key, consumer_key),
                )
                self._history(event_key, consumer_key, "ACKNOWLEDGED", worker, now)
                self._db.commit()
                return True
            except Exception:
                self._db.rollback()
                raise

    def reject(
        self,
        event_id: str,
        consumer: str,
        *,
        worker_id: str,
        error_type: str,
        retry_delay_ms: int = 5_000,
        max_attempts: int = 5,
        now_ms: int | None = None,
    ) -> str:
        """Schedule a retry or move the delivery to DEAD_LETTER."""
        event_key = str(event_id or "").strip()
        consumer_key = str(consumer or "").strip().upper()
        worker = str(worker_id or "").strip()
        if not isinstance(retry_delay_ms, int) or retry_delay_ms < 0:
            raise Challenge1QueueError("retry_delay_ms must be non-negative")
        if not isinstance(max_attempts, int) or max_attempts < 1:
            raise Challenge1QueueError("max_attempts must be positive")
        safe_error = re.sub(r"[^A-Za-z0-9_.-]", "_", str(error_type or "UNKNOWN"))[:80] or "UNKNOWN"
        now = int(now_ms if now_ms is not None else time.time() * 1000)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute(
                    """SELECT status, lease_owner, attempts FROM csr_challenge1_queue_delivery
                       WHERE event_id=? AND consumer=?""",
                    (event_key, consumer_key),
                ).fetchone()
                if row is None or row["status"] != "PROCESSING" or row["lease_owner"] != worker:
                    self._db.rollback()
                    raise Challenge1QueueError("delivery is not leased by this worker")
                dead = int(row["attempts"]) >= max_attempts
                state = "DEAD_LETTER" if dead else "RETRY"
                available = now if dead else now + retry_delay_ms
                self._db.execute(
                    """UPDATE csr_challenge1_queue_delivery
                       SET status=?, available_at_ms=?, lease_owner=NULL, lease_until_ms=NULL,
                           last_error_type=?, updated_at_ms=?
                       WHERE event_id=? AND consumer=?""",
                    (state, available, safe_error, now, event_key, consumer_key),
                )
                self._history(
                    event_key, consumer_key, state, worker, now,
                    {"error_type": safe_error, "attempt": int(row["attempts"])},
                )
                self._db.commit()
                return state
            except Exception:
                self._db.rollback()
                raise

    def delivery_status(self, event_id: str, consumer: str) -> dict[str, Any]:
        row = self._db.execute(
            """SELECT status, attempts, available_at_ms, lease_owner,
                      lease_until_ms, delivered_at_ms, last_error_type
               FROM csr_challenge1_queue_delivery WHERE event_id=? AND consumer=?""",
            (str(event_id or "").strip(), str(consumer or "").strip().upper()),
        ).fetchone()
        if row is None:
            return {"status": "NOT_FOUND"}
        return {
            "status": str(row["status"]),
            "attempts": int(row["attempts"]),
            "available_at_ms": int(row["available_at_ms"]),
            "lease_owner": row["lease_owner"],
            "lease_until_ms": row["lease_until_ms"],
            "delivered_at_ms": row["delivered_at_ms"],
            "last_error_type": row["last_error_type"],
        }

    def history(self, event_id: str) -> list[dict[str, Any]]:
        rows = self._db.execute(
            """SELECT sequence, consumer, state, worker_id, occurred_at_ms, details_json
               FROM csr_challenge1_queue_history WHERE event_id=? ORDER BY sequence ASC""",
            (str(event_id or "").strip(),),
        ).fetchall()
        return [
            {
                "sequence": int(row["sequence"]),
                "consumer": str(row["consumer"]),
                "state": str(row["state"]),
                "worker_id": row["worker_id"],
                "occurred_at_ms": int(row["occurred_at_ms"]),
                "details": json.loads(str(row["details_json"])),
            }
            for row in rows
        ]

    def status_snapshot(self) -> dict[str, Any]:
        rows = self._db.execute(
            """SELECT consumer, status, COUNT(*) AS count
               FROM csr_challenge1_queue_delivery GROUP BY consumer, status
               ORDER BY consumer, status"""
        ).fetchall()
        return {
            "backend": self.backend,
            "counts": [
                {"consumer": str(row["consumer"]), "status": str(row["status"]), "count": int(row["count"])}
                for row in rows
            ],
            "event_count": int(self._db.execute(
                "SELECT COUNT(*) FROM csr_challenge1_queue_events"
            ).fetchone()[0]),
            "append_only_source_events": True,
            "append_only_delivery_history": True,
            "exchange_order_capability": False,
        }
