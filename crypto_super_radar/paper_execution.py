"""Append-only order-state and fill-event ledger for PAPER_ONLY execution.

Event writes are owned by the caller's database transaction so the journal and
paper-account projection commit or roll back atomically. This module never
connects to an exchange and never submits a live order.
"""
from __future__ import annotations

import hashlib
import json
import math
import threading
import time
from typing import Any

ORDER_STATUSES = frozenset({
    "CREATED", "VALIDATED", "SUBMITTED", "ACCEPTED",
    "PARTIALLY_FILLED", "FILLED", "CANCELED", "REJECTED", "UNKNOWN",
})

_ALLOWED_TRANSITIONS = {
    None: {"CREATED"},
    "CREATED": {"VALIDATED", "CANCELED", "REJECTED", "UNKNOWN"},
    "VALIDATED": {"SUBMITTED", "PARTIALLY_FILLED", "FILLED", "CANCELED", "REJECTED", "UNKNOWN"},
    "SUBMITTED": {"ACCEPTED", "PARTIALLY_FILLED", "FILLED", "CANCELED", "REJECTED", "UNKNOWN"},
    "ACCEPTED": {"PARTIALLY_FILLED", "FILLED", "CANCELED", "REJECTED", "UNKNOWN"},
    "PARTIALLY_FILLED": {"PARTIALLY_FILLED", "FILLED", "CANCELED", "UNKNOWN"},
    "UNKNOWN": {"ACCEPTED", "PARTIALLY_FILLED", "FILLED", "CANCELED", "REJECTED", "UNKNOWN"},
    "FILLED": set(), "CANCELED": set(), "REJECTED": set(),
}


class OrderStateTransitionError(ValueError):
    """A requested lifecycle transition violates the order state machine."""


class IdempotencyConflict(ValueError):
    """An idempotency key was reused for different event content."""


def _finite_number(value: Any, field: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be a finite number") from exc
    if not math.isfinite(result) or (minimum is not None and result < minimum):
        raise ValueError(f"{field} must be a finite number >= {minimum}")
    return result


def make_paper_client_order_id(asset: str, signal_bar: str, leg: str) -> str:
    """Stable, deterministic identifier for one paper entry or exit leg."""
    asset_value = str(asset or "").strip()
    bar_value = str(signal_bar or "").strip()
    leg_value = str(leg or "").strip().upper()
    if not asset_value or not bar_value or leg_value not in {"ENTRY", "EXIT"}:
        raise ValueError("asset, signal_bar and ENTRY/EXIT leg are required")
    digest = hashlib.sha256(
        f"{asset_value}|{bar_value}|{leg_value}".encode("utf-8")
    ).hexdigest()[:32]
    return f"paper-{leg_value.lower()}-{digest}"


class PaperExecutionEventLedger:
    """Append-only event journal backed by the caller's DB-API connection."""

    def __init__(self, connection: Any, *, lock: threading.RLock | None = None):
        self._db = connection
        self._lock = lock or threading.RLock()
        with self._lock:
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS paper_execution_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    client_order_id TEXT NOT NULL,
                    asset TEXT NOT NULL,
                    side TEXT NOT NULL,
                    status TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    filled_quantity REAL NOT NULL,
                    fill_price REAL,
                    fee_paid_usd REAL,
                    source TEXT NOT NULL,
                    reason TEXT,
                    occurred_at INTEGER NOT NULL,
                    payload_json TEXT NOT NULL
                )"""
            )
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_paper_execution_order_sequence "
                "ON paper_execution_events(client_order_id, sequence)"
            )
            self._db.execute(
                """CREATE TRIGGER IF NOT EXISTS paper_execution_events_no_update
                BEFORE UPDATE ON paper_execution_events
                BEGIN SELECT RAISE(ABORT, 'paper execution events are append-only'); END"""
            )
            self._db.execute(
                """CREATE TRIGGER IF NOT EXISTS paper_execution_events_no_delete
                BEFORE DELETE ON paper_execution_events
                BEGIN SELECT RAISE(ABORT, 'paper execution events are append-only'); END"""
            )

    def append_event(
        self, *, client_order_id: str, idempotency_key: str, asset: str,
        side: str, status: str, quantity: Any, filled_quantity: Any = 0.0,
        fill_price: Any = None, fee_paid_usd: Any = None,
        source: str = "PAPER_SIMULATOR", reason: str | None = None,
        occurred_at: int | None = None,
    ) -> dict[str, Any]:
        """Append one validated lifecycle event, or return its identical prior copy.

        The caller must include this call in the same transaction as the
        paper position/cash/trade update. The ledger intentionally never commits.
        """
        order_id = str(client_order_id or "").strip()
        key = str(idempotency_key or "").strip()
        asset_value = str(asset or "").strip()
        side_value = str(side or "").strip().upper()
        status_value = str(status or "").strip().upper()
        source_value = str(source or "").strip()
        if not order_id or not key or not asset_value or not source_value:
            raise ValueError("client_order_id, idempotency_key, asset and source are required")
        if side_value not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        if status_value not in ORDER_STATUSES:
            raise ValueError(f"unsupported order status: {status_value}")

        qty = _finite_number(quantity, "quantity", minimum=0.0)
        filled = _finite_number(filled_quantity, "filled_quantity", minimum=0.0)
        if qty <= 0 or filled > qty:
            raise ValueError("quantity must be positive and filled_quantity cannot exceed it")
        fill = None if fill_price is None else _finite_number(fill_price, "fill_price", minimum=0.0)
        if fill is not None and fill <= 0:
            raise ValueError("fill_price must be positive")
        fee = None if fee_paid_usd is None else _finite_number(fee_paid_usd, "fee_paid_usd", minimum=0.0)
        if status_value in {"PARTIALLY_FILLED", "FILLED"}:
            if fill is None or fee is None:
                raise ValueError("fill_price and fee_paid_usd are required for fill events")
            if status_value == "PARTIALLY_FILLED" and not (0 < filled < qty):
                raise ValueError("PARTIALLY_FILLED requires 0 < filled_quantity < quantity")
            if status_value == "FILLED" and not math.isclose(filled, qty, rel_tol=1e-9, abs_tol=1e-12):
                raise ValueError("FILLED requires filled_quantity to equal quantity")
        if status_value in {"CREATED", "VALIDATED", "SUBMITTED", "ACCEPTED", "REJECTED"}:
            if fill is not None or fee is not None:
                raise ValueError(f"{status_value} cannot contain fill price or fee")

        timestamp = int(time.time()) if occurred_at is None else int(occurred_at)
        payload = {
            "client_order_id": order_id, "asset": asset_value, "side": side_value,
            "status": status_value, "quantity": qty, "filled_quantity": filled,
            "fill_price": fill, "fee_paid_usd": fee, "source": source_value,
            "reason": reason, "occurred_at": timestamp,
        }
        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        event_id = hashlib.sha256(key.encode("utf-8")).hexdigest()

        with self._lock:
            duplicate = self._db.execute(
                "SELECT * FROM paper_execution_events WHERE idempotency_key=?", (key,)
            ).fetchone()
            if duplicate is not None:
                if duplicate["payload_json"] != payload_json:
                    raise IdempotencyConflict("idempotency key was already used for different event content")
                return self._as_dict(duplicate, inserted=False)

            previous = self._db.execute(
                "SELECT * FROM paper_execution_events WHERE client_order_id=? "
                "ORDER BY sequence DESC LIMIT 1", (order_id,)
            ).fetchone()
            previous_status = previous["status"] if previous else None
            if status_value not in _ALLOWED_TRANSITIONS[previous_status]:
                raise OrderStateTransitionError(
                    f"invalid order transition: {previous_status or 'NONE'} -> {status_value}"
                )
            if previous is not None:
                if previous["asset"] != asset_value or previous["side"] != side_value:
                    raise OrderStateTransitionError("asset and side cannot change during an order lifecycle")
                if not math.isclose(float(previous["quantity"]), qty, rel_tol=1e-9, abs_tol=1e-12):
                    raise OrderStateTransitionError("order quantity cannot change during an order lifecycle")
                previous_filled = float(previous["filled_quantity"])
                if filled < previous_filled:
                    raise OrderStateTransitionError("cumulative filled quantity cannot decrease")
                if status_value not in {"PARTIALLY_FILLED", "FILLED"} and not math.isclose(
                    filled, previous_filled, rel_tol=1e-9, abs_tol=1e-12
                ):
                    raise OrderStateTransitionError("non-fill events cannot change cumulative filled quantity")
            elif status_value != "CREATED":
                raise OrderStateTransitionError("first order event must be CREATED")

            self._db.execute(
                """INSERT INTO paper_execution_events
                (event_id, idempotency_key, client_order_id, asset, side, status,
                 quantity, filled_quantity, fill_price, fee_paid_usd, source,
                 reason, occurred_at, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (event_id, key, order_id, asset_value, side_value, status_value,
                 qty, filled, fill, fee, source_value, reason, timestamp, payload_json),
            )
            row = self._db.execute(
                "SELECT * FROM paper_execution_events WHERE event_id=?", (event_id,)
            ).fetchone()
            return self._as_dict(row, inserted=True)

    def record_simulated_fill(
        self, *, client_order_id: str, asset: str, side: str, quantity: Any,
        fill_price: Any, fee_paid_usd: Any, source: str, reason: str,
        occurred_at: int | None = None,
    ) -> dict[str, Any]:
        """Record one immediate paper fill as CREATED -> VALIDATED -> FILLED."""
        order_id = str(client_order_id).strip()
        qty = _finite_number(quantity, "quantity", minimum=0.0)
        timestamp = int(time.time()) if occurred_at is None else int(occurred_at)
        prefix = f"{order_id}:"
        self.append_event(
            client_order_id=order_id, idempotency_key=prefix + "created",
            asset=asset, side=side, status="CREATED", quantity=qty,
            source=source, reason=reason, occurred_at=timestamp,
        )
        self.append_event(
            client_order_id=order_id, idempotency_key=prefix + "validated",
            asset=asset, side=side, status="VALIDATED", quantity=qty,
            source=source, reason=reason, occurred_at=timestamp,
        )
        return self.append_event(
            client_order_id=order_id, idempotency_key=prefix + "filled",
            asset=asset, side=side, status="FILLED", quantity=qty,
            filled_quantity=qty, fill_price=fill_price, fee_paid_usd=fee_paid_usd,
            source=source, reason=reason, occurred_at=timestamp,
        )

    def order_state(self, client_order_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM paper_execution_events WHERE client_order_id=? "
                "ORDER BY sequence DESC LIMIT 1", (str(client_order_id),)
            ).fetchone()
        return self._as_dict(row) if row is not None else None

    def recent_events(self, limit: int = 50, *, client_order_id: str | None = None) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 1000))
        with self._lock:
            if client_order_id:
                rows = self._db.execute(
                    "SELECT * FROM paper_execution_events WHERE client_order_id=? "
                    "ORDER BY sequence DESC LIMIT ?", (str(client_order_id), bounded)
                ).fetchall()
            else:
                rows = self._db.execute(
                    "SELECT * FROM paper_execution_events ORDER BY sequence DESC LIMIT ?",
                    (bounded,)
                ).fetchall()
        return [self._as_dict(row) for row in rows]

    def latest_orders(self, limit: int = 20) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 1000))
        with self._lock:
            rows = self._db.execute(
                """SELECT e.* FROM paper_execution_events e
                JOIN (
                    SELECT client_order_id, MAX(sequence) AS latest_sequence
                    FROM paper_execution_events GROUP BY client_order_id
                ) latest ON latest.latest_sequence=e.sequence
                ORDER BY e.sequence DESC LIMIT ?""", (bounded,)
            ).fetchall()
        return [self._as_dict(row) for row in rows]

    def event_count(self) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) AS event_count FROM paper_execution_events"
            ).fetchone()
        return int(row["event_count"])

    @staticmethod
    def _as_dict(row: Any, *, inserted: bool | None = None) -> dict[str, Any]:
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        if inserted is not None:
            result["inserted"] = inserted
        return result
