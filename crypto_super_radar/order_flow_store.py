"""Durable, idempotent SQLite ledger for normalized public order-flow trades.

The ledger stores observations; it does not establish feed health, completeness,
or a usable CVD by itself. Callers must still pass each observation through
VenueOrderFlow and the feed-coverage gate before using any derived signal.
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Any


_REQUIRED = (
    "venue",
    "symbol",
    "trade_id",
    "event_time_ms",
    "received_at_ms",
    "price",
    "base_quantity",
    "aggressor_side",
    "source",
)


def _validate_event(event: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(event, Mapping):
        raise ValueError("event must be a mapping")
    missing = [key for key in _REQUIRED if key not in event]
    if missing:
        raise ValueError("missing required event fields: " + ",".join(missing))

    normalized: dict[str, Any] = {}
    for key in ("venue", "symbol", "trade_id", "source"):
        value = event[key]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key} must be a non-empty string")
        normalized[key] = value.strip()

    side = event["aggressor_side"]
    if not isinstance(side, str) or side.strip().upper() not in {"BUY", "SELL"}:
        raise ValueError("aggressor_side must be BUY or SELL")
    normalized["aggressor_side"] = side.strip().upper()

    for key in ("event_time_ms", "received_at_ms"):
        value = event[key]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{key} must be a positive integer")
        normalized[key] = value

    for key in ("price", "base_quantity"):
        value = event[key]
        if isinstance(value, bool):
            raise ValueError(f"{key} must be a finite positive number")
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"{key} must be a finite positive number") from exc
        if not math.isfinite(number) or number <= 0:
            raise ValueError(f"{key} must be a finite positive number")
        normalized[key] = number
    return normalized


class OrderFlowEventStore:
    """Append-only event ledger backed by a caller-owned SQLite-compatible connection."""

    def __init__(self, connection: Any) -> None:
        if connection is None or not callable(getattr(connection, "execute", None)):
            raise ValueError("a SQLite-compatible connection is required")
        self._connection = connection
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS order_flow_events (
                venue TEXT NOT NULL,
                symbol TEXT NOT NULL,
                trade_id TEXT NOT NULL,
                event_time_ms INTEGER NOT NULL,
                received_at_ms INTEGER NOT NULL,
                price REAL NOT NULL,
                base_quantity REAL NOT NULL,
                aggressor_side TEXT NOT NULL CHECK (aggressor_side IN ('BUY', 'SELL')),
                source TEXT NOT NULL,
                PRIMARY KEY (venue, symbol, trade_id)
            )
            """
        )
        self._connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_order_flow_events_time
            ON order_flow_events (venue, symbol, event_time_ms)
            """
        )
        self._connection.commit()

    def append(self, event: Mapping[str, Any]) -> bool:
        """Persist one normalized observation; return False for a duplicate ID."""
        return self.append_many([event])["inserted"] == 1

    def append_many(self, events: Iterable[Mapping[str, Any]]) -> dict[str, int]:
        """Validate the whole batch before writing; duplicates are idempotent."""
        if isinstance(events, (str, bytes, Mapping)):
            raise ValueError("events must be an iterable of event mappings")
        normalized = [_validate_event(event) for event in events]
        if not normalized:
            return {"inserted": 0, "duplicates": 0}

        inserted = 0
        cursor = self._connection.cursor()
        try:
            for event in normalized:
                cursor.execute(
                    """
                    INSERT OR IGNORE INTO order_flow_events (
                        venue, symbol, trade_id, event_time_ms, received_at_ms,
                        price, base_quantity, aggressor_side, source
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event["venue"], event["symbol"], event["trade_id"],
                        event["event_time_ms"], event["received_at_ms"],
                        event["price"], event["base_quantity"],
                        event["aggressor_side"], event["source"],
                    ),
                )
                inserted += max(0, int(cursor.rowcount or 0))
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        return {"inserted": inserted, "duplicates": len(normalized) - inserted}

    def read_window(
        self,
        *,
        venue: str,
        symbol: str,
        start_event_time_ms: int,
        end_event_time_ms: int,
        limit: int = 50_000,
    ) -> list[dict[str, Any]]:
        """Read ordered observations; does not certify continuity or coverage."""
        if not isinstance(venue, str) or not venue.strip():
            raise ValueError("venue is required")
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("symbol is required")
        for name, value in (
            ("start_event_time_ms", start_event_time_ms),
            ("end_event_time_ms", end_event_time_ms),
            ("limit", limit),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if start_event_time_ms > end_event_time_ms or limit == 0:
            return []
        cursor = self._connection.execute(
            """
            SELECT venue, symbol, trade_id, event_time_ms, received_at_ms,
                   price, base_quantity, aggressor_side, source
            FROM order_flow_events
            WHERE venue = ? AND symbol = ?
              AND event_time_ms >= ? AND event_time_ms <= ?
            ORDER BY event_time_ms ASC, trade_id ASC
            LIMIT ?
            """,
            (venue.strip(), symbol.strip().upper(), start_event_time_ms,
             end_event_time_ms, limit),
        )
        columns = [column[0] for column in (cursor.description or ())]
        rows = cursor.fetchall()
        normalized_rows: list[dict[str, Any]] = []
        for row in rows:
            if isinstance(row, Mapping):
                normalized_rows.append(dict(row))
            else:
                normalized_rows.append(dict(zip(columns, row)))
        return normalized_rows
