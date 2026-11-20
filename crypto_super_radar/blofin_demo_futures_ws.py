"""Read-only BloFin Demo USDT-Futures public WebSocket ingestion.

This module uses only the Demo public WebSocket endpoint. It never authenticates,
submits orders, modifies account state, or consumes private channels. Futures
trade sizes are contracts; a validated contract_value is required before
converting them to base-asset quantity for the existing order-flow ledger.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
import sqlite3
import time
from collections import Counter
from collections.abc import AsyncIterable, Mapping
from typing import Any, Callable

from .blofin_demo_readonly import DEMO_PUBLIC_WS_URL


DEFAULT_PUBLIC_CHANNELS = (
    "trades",
    "books5",
    "tickers",
    "funding-rate",
    "mark-price-candle1m",
)
_ALLOWED_CHANNELS = frozenset(DEFAULT_PUBLIC_CHANNELS)


class BloFinDemoWebSocketError(RuntimeError):
    """BloFin public WebSocket protocol, payload or durability failure."""


def _positive_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or value is None:
        raise BloFinDemoWebSocketError(f"INVALID_{name.upper()}")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise BloFinDemoWebSocketError(f"INVALID_{name.upper()}") from exc
    if not math.isfinite(number) or number <= 0:
        raise BloFinDemoWebSocketError(f"INVALID_{name.upper()}")
    return number


def _finite_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or value is None:
        raise BloFinDemoWebSocketError(f"INVALID_{name.upper()}")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise BloFinDemoWebSocketError(f"INVALID_{name.upper()}") from exc
    if not math.isfinite(number):
        raise BloFinDemoWebSocketError(f"INVALID_{name.upper()}")
    return number


def _positive_timestamp_ms(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise BloFinDemoWebSocketError(f"INVALID_{name.upper()}")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and value.isdigit():
        result = int(value)
    else:
        raise BloFinDemoWebSocketError(f"INVALID_{name.upper()}")
    if result <= 0:
        raise BloFinDemoWebSocketError(f"INVALID_{name.upper()}")
    return result


def _symbol(value: Any, expected: str, name: str = "instrument") -> str:
    if not isinstance(value, str) or value.strip().upper() != expected:
        raise BloFinDemoWebSocketError(f"{name.upper()}_MISMATCH")
    return expected


def _event_id(channel: str, identity: Any) -> str:
    raw = json.dumps(
        {"channel": channel, "identity": identity},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_public_subscription(inst_id: str, channels: tuple[str, ...] = DEFAULT_PUBLIC_CHANNELS) -> dict[str, Any]:
    """Build an allow-listed public subscription; never accepts private channels."""
    instrument = str(inst_id or "").strip().upper()
    if not instrument.endswith("-USDT") or not instrument.isascii():
        raise ValueError("Only normalized BloFin USDT linear instrument IDs are supported.")
    if any(not (part.isalnum()) for part in instrument.split("-")):
        raise ValueError("Instrument ID contains invalid characters.")
    selected = tuple(channels)
    if not selected or len(set(selected)) != len(selected):
        raise ValueError("Public channel list must be non-empty and contain no duplicates.")
    if any(channel not in _ALLOWED_CHANNELS for channel in selected):
        raise ValueError("Only documented public futures channels are allowed.")
    args = [{"channel": channel, "instId": instrument} for channel in selected]
    encoded = json.dumps({"op": "subscribe", "args": args}, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 4096:
        raise ValueError("WebSocket subscription exceeds BloFin's 4096-byte limit.")
    return {"op": "subscribe", "args": args}


def _one_row(data: Any, label: str) -> Mapping[str, Any]:
    if isinstance(data, Mapping):
        return data
    if isinstance(data, list) and len(data) == 1 and isinstance(data[0], Mapping):
        return data[0]
    raise BloFinDemoWebSocketError(f"{label.upper()}_DATA_NOT_UNIQUE")


def _normalize_book_levels(rows: Any, side: str) -> list[list[float]]:
    if not isinstance(rows, list) or not rows or len(rows) > 5:
        raise BloFinDemoWebSocketError(f"INVALID_{side.upper()}_DEPTH")
    levels: list[list[float]] = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            raise BloFinDemoWebSocketError(f"INVALID_{side.upper()}_LEVEL")
        levels.append([
            _positive_number(row[0], f"{side}_price"),
            _positive_number(row[1], f"{side}_size"),
        ])
    return levels


def normalize_blofin_demo_futures_message(
    raw: Any,
    *,
    inst_id: str,
    received_at_ms: int,
    contract_value: float,
) -> dict[str, Any]:
    """Normalize one Demo futures public WS message without inferring missing units."""
    instrument = str(inst_id or "").strip().upper()
    if not instrument.endswith("-USDT"):
        raise ValueError("Only USDT-margined BloFin futures are supported.")
    receive_time = _positive_timestamp_ms(received_at_ms, "receive_timestamp")
    multiplier = _positive_number(contract_value, "contract_value")

    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    if isinstance(raw, str):
        if raw.strip().lower() in {"pong", "ping"}:
            return {"type": "heartbeat", "message": raw.strip().lower(), "events": []}
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError, UnicodeDecodeError) as exc:
            raise BloFinDemoWebSocketError("INVALID_JSON_MESSAGE") from exc
    else:
        payload = raw
    if not isinstance(payload, Mapping):
        raise BloFinDemoWebSocketError("INVALID_WEBSOCKET_ENVELOPE")

    event = str(payload.get("event") or "").strip().lower()
    if event == "error":
        code = str(payload.get("code") or "UNKNOWN")
        raise BloFinDemoWebSocketError(f"SUBSCRIPTION_ERROR_{code}")
    if event in {"subscribe", "unsubscribe"}:
        arg = payload.get("arg")
        if not isinstance(arg, Mapping):
            raise BloFinDemoWebSocketError("INVALID_SUBSCRIPTION_ACK")
        _symbol(arg.get("instId"), instrument, "ack_instrument")
        channel = str(arg.get("channel") or "")
        if channel not in _ALLOWED_CHANNELS:
            raise BloFinDemoWebSocketError("UNEXPECTED_SUBSCRIPTION_CHANNEL")
        return {"type": "control", "event": event, "channel": channel, "events": []}

    arg = payload.get("arg")
    if not isinstance(arg, Mapping):
        raise BloFinDemoWebSocketError("MISSING_CHANNEL_ARGUMENT")
    channel = str(arg.get("channel") or "")
    if channel not in _ALLOWED_CHANNELS:
        raise BloFinDemoWebSocketError("UNSUPPORTED_PUBLIC_CHANNEL")
    _symbol(arg.get("instId"), instrument, "channel_instrument")
    data = payload.get("data")
    events: list[dict[str, Any]] = []

    if channel == "trades":
        row = _one_row(data, "trade")
        _symbol(row.get("instId"), instrument, "trade_instrument")
        trade_id = row.get("tradeId")
        if trade_id is None or isinstance(trade_id, bool) or not str(trade_id).strip():
            raise BloFinDemoWebSocketError("MISSING_TRADE_ID")
        trade_price = _positive_number(row.get("price"), "trade_price")
        contract_size = _positive_number(row.get("size"), "contract_size")
        side = str(row.get("side") or "").strip().lower()
        if side not in {"buy", "sell"}:
            raise BloFinDemoWebSocketError("INVALID_TRADE_SIDE")
        trade_time = _positive_timestamp_ms(row.get("ts"), "trade_timestamp")
        base_quantity = contract_size * multiplier
        if not math.isfinite(base_quantity) or base_quantity <= 0:
            raise BloFinDemoWebSocketError("INVALID_BASE_QUANTITY")
        payload_data = {
            "trade_id": str(trade_id).strip(),
            "price": trade_price,
            "contract_size": contract_size,
            "contract_value": multiplier,
            "base_quantity": base_quantity,
            "aggressor_side": side.upper(),
        }
        events.append({
            "event_id": _event_id(channel, [str(trade_id).strip()]),
            "inst_id": instrument,
            "channel": channel,
            "event_time_ms": trade_time,
            "received_at_ms": receive_time,
            "payload": payload_data,
            "source": "blofin_demo_futures_public_trades",
        })

    elif channel == "books5":
        row = _one_row(data, "books5")
        asks = _normalize_book_levels(row.get("asks"), "ask")
        bids = _normalize_book_levels(row.get("bids"), "bid")
        timestamp = _positive_timestamp_ms(row.get("ts"), "book_timestamp")
        if asks[0][0] < bids[0][0]:
            raise BloFinDemoWebSocketError("CROSSED_BLOFIN_BOOK")
        stable = {"asks": asks, "bids": bids, "ts": timestamp}
        events.append({
            "event_id": _event_id(channel, stable),
            "inst_id": instrument,
            "channel": channel,
            "event_time_ms": timestamp,
            "received_at_ms": receive_time,
            "payload": stable,
            "source": "blofin_demo_futures_public_books5",
        })

    elif channel == "tickers":
        row = _one_row(data, "ticker")
        _symbol(row.get("instId"), instrument, "ticker_instrument")
        last = _positive_number(row.get("last"), "last_price")
        bid = _positive_number(row.get("bidPrice"), "bid_price")
        ask = _positive_number(row.get("askPrice"), "ask_price")
        if ask < bid:
            raise BloFinDemoWebSocketError("CROSSED_BLOFIN_TICKER")
        timestamp = _positive_timestamp_ms(row.get("ts"), "ticker_timestamp")
        stable = {
            "last": last,
            "bid_price": bid,
            "ask_price": ask,
            "last_size": _positive_number(row.get("lastSize"), "last_size"),
            "bid_size": _positive_number(row.get("bidSize"), "bid_size"),
            "ask_size": _positive_number(row.get("askSize"), "ask_size"),
            "ts": timestamp,
        }
        events.append({
            "event_id": _event_id(channel, stable),
            "inst_id": instrument,
            "channel": channel,
            "event_time_ms": timestamp,
            "received_at_ms": receive_time,
            "payload": stable,
            "source": "blofin_demo_futures_public_tickers",
        })

    elif channel == "funding-rate":
        row = _one_row(data, "funding_rate")
        _symbol(row.get("instId"), instrument, "funding_instrument")
        funding_rate = _finite_number(row.get("fundingRate"), "funding_rate")
        settlement_time = _positive_timestamp_ms(row.get("fundingTime"), "funding_settlement_time")
        stable = {
            "funding_rate": funding_rate,
            "upcoming_settlement_time_ms": settlement_time,
            "timestamp_semantics": "UPCOMING_SETTLEMENT_NOT_EVENT_TIME",
        }
        events.append({
            "event_id": _event_id(channel, stable),
            "inst_id": instrument,
            "channel": channel,
            "event_time_ms": None,
            "received_at_ms": receive_time,
            "payload": stable,
            "source": "blofin_demo_futures_public_funding_rate",
        })

    elif channel == "mark-price-candle1m":
        rows = data if isinstance(data, list) else [data]
        if not rows:
            raise BloFinDemoWebSocketError("EMPTY_MARK_PRICE_CANDLE_BATCH")
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) < 6:
                raise BloFinDemoWebSocketError("INVALID_MARK_PRICE_CANDLE_ROW")
            timestamp = _positive_timestamp_ms(row[0], "mark_candle_timestamp")
            open_price = _positive_number(row[1], "mark_candle_open")
            high_price = _positive_number(row[2], "mark_candle_high")
            low_price = _positive_number(row[3], "mark_candle_low")
            close_price = _positive_number(row[4], "mark_candle_close")
            if high_price < max(open_price, low_price, close_price) or low_price > min(open_price, close_price):
                raise BloFinDemoWebSocketError("INCONSISTENT_MARK_PRICE_CANDLE")
            confirm = str(row[5]).strip()
            if confirm not in {"0", "1"}:
                raise BloFinDemoWebSocketError("INVALID_MARK_CANDLE_CONFIRM")
            stable = {
                "open_time_ms": timestamp,
                "open": open_price,
                "high": high_price,
                "low": low_price,
                "close": close_price,
                "confirmed": confirm == "1",
            }
            events.append({
                "event_id": _event_id(channel, stable),
                "inst_id": instrument,
                "channel": channel,
                "event_time_ms": timestamp,
                "received_at_ms": receive_time,
                "payload": stable,
                "source": "blofin_demo_futures_public_mark_price_candle1m",
            })

    return {"type": "data", "channel": channel, "events": events}


class BloFinDemoFuturesPublicEventStore:
    """Append-only SQLite/Turso-compatible store for typed public Demo observations."""

    def __init__(self, connection: Any) -> None:
        if connection is None or not callable(getattr(connection, "execute", None)):
            raise ValueError("a SQLite-compatible connection is required")
        self._connection = connection
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS blofin_demo_futures_public_events (
                inst_id TEXT NOT NULL,
                channel TEXT NOT NULL,
                event_id TEXT NOT NULL,
                event_time_ms INTEGER,
                received_at_ms INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                source TEXT NOT NULL,
                PRIMARY KEY (inst_id, channel, event_id)
            )
            """
        )
        self._connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS blofin_demo_futures_public_events_no_update
            BEFORE UPDATE ON blofin_demo_futures_public_events
            BEGIN SELECT RAISE(ABORT, 'BloFin Demo public futures events are append-only'); END
            """
        )
        self._connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS blofin_demo_futures_public_events_no_delete
            BEFORE DELETE ON blofin_demo_futures_public_events
            BEGIN SELECT RAISE(ABORT, 'BloFin Demo public futures events are append-only'); END
            """
        )
        self._connection.commit()

    def append_many(self, events: list[Mapping[str, Any]]) -> dict[str, int]:
        if not isinstance(events, list):
            raise ValueError("events must be a list")
        normalized = []
        for event in events:
            if not isinstance(event, Mapping):
                raise ValueError("event must be a mapping")
            inst_id = str(event.get("inst_id") or "").strip().upper()
            channel = str(event.get("channel") or "")
            event_id = str(event.get("event_id") or "").strip()
            received = event.get("received_at_ms")
            source = str(event.get("source") or "").strip()
            payload = event.get("payload")
            event_time = event.get("event_time_ms")
            if not inst_id or channel not in _ALLOWED_CHANNELS or not event_id or not source:
                raise ValueError("event identity or source is invalid")
            if isinstance(received, bool) or not isinstance(received, int) or received <= 0:
                raise ValueError("received_at_ms must be a positive integer")
            if event_time is not None and (
                isinstance(event_time, bool) or not isinstance(event_time, int) or event_time <= 0
            ):
                raise ValueError("event_time_ms must be null or a positive integer")
            if not isinstance(payload, Mapping):
                raise ValueError("payload must be a mapping")
            payload_json = json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), allow_nan=False)
            normalized.append((inst_id, channel, event_id, event_time, received, payload_json, source))
        if not normalized:
            return {"inserted": 0, "duplicates": 0}
        inserted = 0
        cursor = self._connection.cursor()
        try:
            for row in normalized:
                cursor.execute(
                    """INSERT OR IGNORE INTO blofin_demo_futures_public_events
                    (inst_id, channel, event_id, event_time_ms, received_at_ms, payload_json, source)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    row,
                )
                inserted += max(0, int(cursor.rowcount or 0))
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        return {"inserted": inserted, "duplicates": len(normalized) - inserted}

    def read_recent(self, *, inst_id: str, channel: str, limit: int = 100) -> list[dict[str, Any]]:
        if channel not in _ALLOWED_CHANNELS or not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
            raise ValueError("channel or limit is invalid")
        cursor = self._connection.execute(
            """SELECT inst_id, channel, event_id, event_time_ms, received_at_ms, payload_json, source
               FROM blofin_demo_futures_public_events
               WHERE inst_id=? AND channel=?
               ORDER BY received_at_ms DESC LIMIT ?""",
            (str(inst_id).strip().upper(), channel, limit),
        )
        result = []
        for row in cursor.fetchall():
            if isinstance(row, Mapping):
                item = dict(row)
            else:
                item = dict(zip(
                    ("inst_id","channel","event_id","event_time_ms","received_at_ms","payload_json","source"),
                    row,
                ))
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result


async def demo_futures_public_websocket_stream(
    inst_id: str,
    *,
    channels: tuple[str, ...] = DEFAULT_PUBLIC_CHANNELS,
    heartbeat_interval_seconds: float = 15.0,
    pong_timeout_seconds: float = 10.0,
) -> AsyncIterable[Any]:
    """Open BloFin Demo's public futures WS; heartbeat uses text ping/pong."""
    subscription = build_public_subscription(inst_id, channels)
    if not (1 <= heartbeat_interval_seconds < 30 and 0 < pong_timeout_seconds < 30):
        raise ValueError("Heartbeat and pong timeout must be positive and below 30 seconds.")
    try:
        import websockets
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("websockets package is required for BloFin Demo futures streams") from exc

    async with websockets.connect(
        DEMO_PUBLIC_WS_URL,
        ping_interval=None,
        close_timeout=5,
        open_timeout=15,
        max_queue=2000,
    ) as socket:
        await socket.send(json.dumps(subscription, separators=(",", ":")))
        last_receive = time.monotonic()
        pong_deadline: float | None = None
        while True:
            now = time.monotonic()
            if pong_deadline is not None:
                remaining = pong_deadline - now
                if remaining <= 0:
                    raise TimeoutError("BloFin Demo WebSocket did not return text pong.")
                timeout = min(heartbeat_interval_seconds, remaining)
            else:
                timeout = max(0.1, heartbeat_interval_seconds - (now - last_receive))
            try:
                raw = await asyncio.wait_for(socket.recv(), timeout=timeout)
            except asyncio.TimeoutError:
                now = time.monotonic()
                if pong_deadline is not None:
                    if now >= pong_deadline:
                        raise TimeoutError("BloFin Demo WebSocket did not return text pong.")
                    continue
                await socket.send("ping")
                pong_deadline = time.monotonic() + pong_timeout_seconds
                continue
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            if isinstance(raw, str) and raw.strip().lower() == "ping":
                await socket.send("pong")
                last_receive = time.monotonic()
                continue
            if isinstance(raw, str) and raw.strip().lower() == "pong":
                pong_deadline = None
                last_receive = time.monotonic()
                continue
            last_receive = time.monotonic()
            yield raw


class BloFinDemoFuturesPublicWorker:
    """Reconnect-safe public Demo WS worker; no order or private-channel authority."""

    def __init__(
        self,
        *,
        inst_id: str,
        contract_value: float,
        event_store: BloFinDemoFuturesPublicEventStore,
        connector: Callable[[], Any] | None = None,
        clock_ms: Callable[[], int] | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
        base_backoff_seconds: float = 1.0,
        max_backoff_seconds: float = 30.0,
        max_attempts: int | None = None,
        channels: tuple[str, ...] = DEFAULT_PUBLIC_CHANNELS,
    ) -> None:
        instrument = str(inst_id or "").strip().upper()
        build_public_subscription(instrument, channels)
        self.inst_id = instrument
        self.contract_value = _positive_number(contract_value, "contract_value")
        if event_store is None or not callable(getattr(event_store, "append_many", None)):
            raise ValueError("a durable public futures event store is required")
        if not (0 < base_backoff_seconds <= max_backoff_seconds):
            raise ValueError("backoff values must be positive and max must be >= base")
        if max_attempts is not None and (
            isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1
        ):
            raise ValueError("max_attempts must be a positive integer or null")
        self.event_store = event_store
        self.channels = tuple(channels)
        self.connector = connector or (
            lambda: demo_futures_public_websocket_stream(instrument, channels=self.channels)
        )
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.sleep = sleep
        self.base_backoff_seconds = float(base_backoff_seconds)
        self.max_backoff_seconds = float(max_backoff_seconds)
        self.max_attempts = max_attempts
        self._counts: Counter[str] = Counter()
        self._channel_counts: Counter[str] = Counter()
        self._ack_counts: Counter[str] = Counter()
        self._last_error: str | None = None

    @staticmethod
    def _decode(raw: Any) -> Any:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        if isinstance(raw, str):
            return json.loads(raw)
        return raw

    async def run(self, stop_event: Any) -> dict[str, Any]:
        if stop_event is None or not callable(getattr(stop_event, "is_set", None)):
            raise ValueError("stop_event with is_set() is required")
        while not stop_event.is_set():
            if self.max_attempts is not None and self._counts["attempts"] >= self.max_attempts:
                break
            self._counts["attempts"] += 1
            stream = None
            try:
                stream = self.connector()
                if inspect.isawaitable(stream):
                    stream = await stream
                if not hasattr(stream, "__aiter__"):
                    raise TypeError("BloFin connector must return an async iterable")
                async for raw in stream:
                    if stop_event.is_set():
                        break
                    received = self.clock_ms()
                    decoded = self._decode(raw)
                    normalized = normalize_blofin_demo_futures_message(
                        decoded,
                        inst_id=self.inst_id,
                        received_at_ms=received,
                        contract_value=self.contract_value,
                    )
                    self._counts["messages"] += 1
                    if normalized["type"] in {"heartbeat", "control"}:
                        self._counts["control_messages"] += 1
                        if normalized["type"] == "control" and normalized.get("event") == "subscribe":
                            self._ack_counts[str(normalized.get("channel") or "")] += 1
                        continue
                    events = normalized["events"]
                    if not events:
                        raise BloFinDemoWebSocketError("EMPTY_NORMALIZED_EVENT_BATCH")
                    persisted = self.event_store.append_many(events)
                    expected = len(events)
                    if not isinstance(persisted, Mapping) or (
                        persisted.get("inserted", 0) + persisted.get("duplicates", 0) != expected
                    ):
                        raise BloFinDemoWebSocketError("PUBLIC_EVENT_PERSISTENCE_NOT_CONFIRMED")
                    self._counts["observations"] += int(persisted.get("inserted", 0))
                    self._counts["duplicates"] += int(persisted.get("duplicates", 0))
                    self._channel_counts[normalized["channel"]] += expected
                self._last_error = "STREAM_ENDED"
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {str(exc)[:200]}"
            finally:
                close = getattr(stream, "aclose", None)
                if callable(close):
                    try:
                        await close()
                    except Exception:
                        pass
            if stop_event.is_set():
                break
            if self.max_attempts is not None and self._counts["attempts"] >= self.max_attempts:
                break
            delay = min(
                self.base_backoff_seconds * (2 ** (self._counts["attempts"] - 1)),
                self.max_backoff_seconds,
            )
            await self.sleep(delay)
        return {
            "status": "STOPPED" if stop_event.is_set() else "MAX_ATTEMPTS_REACHED",
            "environment": "BLOFIN_DEMO_PUBLIC_WS",
            "instrument_id": self.inst_id,
            "attempts": self._counts["attempts"],
            "messages": self._counts["messages"],
            "control_messages": self._counts["control_messages"],
            "observations": self._counts["observations"],
            "duplicates": self._counts["duplicates"],
            "channel_observation_counts": dict(self._channel_counts),
            "subscription_ack_counts": dict(self._ack_counts),
            "last_error": self._last_error,
            "socket_status": "DISCONNECTED",
            "authentication": "NOT_USED_PUBLIC_CHANNELS_ONLY",
            "trade_authorization": "NO_TRADE",
            "orders_submitted": False,
        }
