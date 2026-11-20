"""Read-only BloFin Demo private Futures WebSocket observation worker.

Only the authenticated Demo private WS endpoint is used. This worker can login
and subscribe to positions, orders and orders-algo; it has no order, cancel,
close, amend, transfer or withdrawal methods. It records observations only.
The existing REST readback remains the authority for current orders and exact
TP/SL verification because BloFin does not send initial snapshots on the
orders and orders-algo channels.
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

from .blofin_demo_readonly import BloFinDemoReadOnly, DEMO_PRIVATE_WS_URL


PRIVATE_CHANNELS = ("positions", "orders", "orders-algo")
DEFAULT_PRIVATE_SYMBOLS = ("BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT", "LINK-USDT")


class BloFinDemoPrivateWSError(RuntimeError):
    """Private Demo WebSocket handshake, protocol or payload failure."""


def _clean_inst_id(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("instrument ID must be a string")
    result = value.strip().upper()
    if not result.endswith("-USDT") or not result.isascii():
        raise ValueError("only normalized USDT linear instruments are supported")
    if any(not part.isalnum() for part in result.split("-")):
        raise ValueError("instrument ID contains invalid characters")
    return result


def _timestamp_ms(value: Any, name: str, *, optional: bool = False) -> int | None:
    if optional and value in (None, ""):
        return None
    if isinstance(value, bool):
        raise BloFinDemoPrivateWSError(f"INVALID_{name.upper()}")
    if isinstance(value, int):
        stamp = value
    elif isinstance(value, str) and value.isdigit():
        stamp = int(value)
    else:
        raise BloFinDemoPrivateWSError(f"INVALID_{name.upper()}")
    if stamp <= 0:
        raise BloFinDemoPrivateWSError(f"INVALID_{name.upper()}")
    return stamp


def _number(
    value: Any,
    name: str,
    *,
    allow_zero: bool = False,
    allow_negative: bool = False,
) -> float:
    if isinstance(value, bool) or value is None:
        raise BloFinDemoPrivateWSError(f"INVALID_{name.upper()}")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise BloFinDemoPrivateWSError(f"INVALID_{name.upper()}") from exc
    if (
        not math.isfinite(result)
        or (not allow_negative and result < 0)
        or (not allow_zero and result == 0)
    ):
        raise BloFinDemoPrivateWSError(f"INVALID_{name.upper()}")
    return result


def _event_id(channel: str, inst_id: str, identity: Any) -> str:
    canonical = json.dumps(
        {"channel": channel, "inst_id": inst_id, "identity": identity},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_private_subscription(inst_ids: tuple[str, ...] = DEFAULT_PRIVATE_SYMBOLS) -> dict[str, Any]:
    """Subscribe only to the three allow-listed private Futures observation channels."""
    if not isinstance(inst_ids, tuple) or not inst_ids:
        raise ValueError("a non-empty tuple of instrument IDs is required")
    normalized = tuple(_clean_inst_id(item) for item in inst_ids)
    if len(set(normalized)) != len(normalized):
        raise ValueError("duplicate instrument IDs are not allowed")
    args = [
        {"channel": channel, "instId": inst_id}
        for inst_id in normalized
        for channel in PRIVATE_CHANNELS
    ]
    encoded = json.dumps({"op": "subscribe", "args": args}, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 4096:
        raise ValueError("private WebSocket subscription exceeds BloFin's 4096-byte limit")
    return {"op": "subscribe", "args": args}


def normalize_blofin_private_futures_message(
    payload: Any,
    *,
    expected_symbols: tuple[str, ...],
    received_at_ms: int,
) -> dict[str, Any]:
    """Validate and normalize one private Demo WS envelope without creating commands."""
    if not isinstance(payload, Mapping):
        raise BloFinDemoPrivateWSError("INVALID_PRIVATE_WS_ENVELOPE")
    received = _timestamp_ms(received_at_ms, "receive_timestamp")
    assert received is not None

    event = str(payload.get("event") or "").strip().lower()
    if event == "login":
        if str(payload.get("code")) != "0":
            raise BloFinDemoPrivateWSError("PRIVATE_WS_LOGIN_REJECTED")
        return {"type": "control", "event": "login", "events": []}
    if event == "subscribe":
        arg = payload.get("arg")
        if not isinstance(arg, Mapping):
            raise BloFinDemoPrivateWSError("INVALID_PRIVATE_SUBSCRIPTION_ACK")
        channel = str(arg.get("channel") or "")
        inst_id = _clean_inst_id(arg.get("instId"))
        if channel not in PRIVATE_CHANNELS or inst_id not in expected_symbols:
            raise BloFinDemoPrivateWSError("UNEXPECTED_PRIVATE_SUBSCRIPTION_ACK")
        return {
            "type": "control",
            "event": "subscribe",
            "channel": channel,
            "inst_id": inst_id,
            "events": [],
        }
    if event == "error":
        code = str(payload.get("code") or "UNKNOWN")
        raise BloFinDemoPrivateWSError(f"PRIVATE_WS_SERVER_ERROR_{code}")

    arg = payload.get("arg")
    if not isinstance(arg, Mapping):
        raise BloFinDemoPrivateWSError("PRIVATE_WS_MISSING_CHANNEL_ARGUMENT")
    channel = str(arg.get("channel") or "")
    if channel not in PRIVATE_CHANNELS:
        raise BloFinDemoPrivateWSError("PRIVATE_WS_CHANNEL_NOT_ALLOWLISTED")
    data = payload.get("data")
    if not isinstance(data, list):
        raise BloFinDemoPrivateWSError("PRIVATE_WS_DATA_NOT_A_LIST")
    action = str(payload.get("action") or "").strip().lower()
    if action and action not in {"snapshot", "update"}:
        raise BloFinDemoPrivateWSError("PRIVATE_WS_INVALID_ACTION")

    arg_inst = arg.get("instId")
    events: list[dict[str, Any]] = []
    if not data:
        if channel != "positions" or not arg_inst:
            raise BloFinDemoPrivateWSError("EMPTY_PRIVATE_DATA_NOT_A_POSITION_SNAPSHOT")
        inst_id = _clean_inst_id(arg_inst)
        if inst_id not in expected_symbols:
            raise BloFinDemoPrivateWSError("PRIVATE_WS_UNEXPECTED_INSTRUMENT")
        events.append({
            "event_id": _event_id(channel, inst_id, {
                "action": action or "initial_or_periodic_snapshot",
                "empty": True,
                "received_at_ms": received,
            }),
            "inst_id": inst_id,
            "channel": channel,
            "action": action or "initial_or_periodic_snapshot",
            "event_time_ms": None,
            "received_at_ms": received,
            "payload": {"items": [], "empty_snapshot": True},
            "source": "blofin_demo_private_positions_ws",
        })
        return {"type": "data", "channel": channel, "events": events}

    for row in data:
        if not isinstance(row, Mapping):
            raise BloFinDemoPrivateWSError("PRIVATE_WS_ROW_NOT_AN_OBJECT")
        inst_id = _clean_inst_id(row.get("instId"))
        if inst_id not in expected_symbols or (arg_inst and _clean_inst_id(arg_inst) != inst_id):
            raise BloFinDemoPrivateWSError("PRIVATE_WS_UNEXPECTED_INSTRUMENT")
        update_time = _timestamp_ms(row.get("updateTime"), "update_time")
        if channel == "positions":
            if str(row.get("instType") or "").upper() != "SWAP":
                raise BloFinDemoPrivateWSError("PRIVATE_WS_POSITION_NOT_SWAP")
            margin_mode = str(row.get("marginMode") or "").lower()
            position_side = str(row.get("positionSide") or "").lower()
            position_id = str(row.get("positionId") or "").strip()
            if margin_mode not in {"cross", "isolated"}:
                raise BloFinDemoPrivateWSError("PRIVATE_WS_INVALID_MARGIN_MODE")
            if position_side not in {"long", "short", "net"} or not position_id:
                raise BloFinDemoPrivateWSError("PRIVATE_WS_POSITION_IDENTITY_INVALID")
            position_size = _number(row.get("positions"), "positions", allow_zero=True, allow_negative=True)
            payload_data = {
                "position_id": position_id,
                "margin_mode": margin_mode,
                "position_side": position_side,
                "positions": str(row.get("positions")),
                "available_positions": str(row.get("availablePositions") or ""),
                "average_price": str(row.get("averagePrice") or ""),
                "unrealized_pnl": str(row.get("unrealizedPnl") or ""),
                "mark_price": str(row.get("markPrice") or ""),
                "leverage": str(row.get("leverage") or ""),
                "position_size_numeric": position_size,
                "update_time_ms": update_time,
            }
            identity = [position_id, update_time, payload_data["positions"], payload_data["available_positions"]]
        elif channel == "orders":
            if str(row.get("instType") or "").upper() != "SWAP":
                raise BloFinDemoPrivateWSError("PRIVATE_WS_ORDER_NOT_SWAP")
            order_id = str(row.get("orderId") or "").strip()
            state = str(row.get("state") or "").strip().lower()
            if not order_id or not state:
                raise BloFinDemoPrivateWSError("PRIVATE_WS_ORDER_IDENTITY_INVALID")
            payload_data = {
                "order_id": order_id,
                "client_order_id": str(row.get("clientOrderId") or ""),
                "position_id": str(row.get("positionId") or ""),
                "size": str(row.get("size") or ""),
                "filled_size": str(row.get("filledSize") or ""),
                "average_price": str(row.get("averagePrice") or ""),
                "side": str(row.get("side") or ""),
                "position_side": str(row.get("positionSide") or ""),
                "margin_mode": str(row.get("marginMode") or ""),
                "state": state,
                "order_category": str(row.get("orderCategory") or ""),
                "tp_trigger_price": str(row.get("tpTriggerPrice") or ""),
                "sl_trigger_price": str(row.get("slTriggerPrice") or ""),
                "tp_trigger_type": str(row.get("tpTriggerPriceType") or ""),
                "sl_trigger_type": str(row.get("slTriggerPriceType") or ""),
                "reduce_only": str(row.get("reduceOnly") or ""),
                "update_time_ms": update_time,
            }
            identity = [order_id, update_time, state, payload_data["filled_size"]]
        else:
            if str(row.get("instType") or "").upper() != "SWAP":
                raise BloFinDemoPrivateWSError("PRIVATE_WS_ALGO_NOT_SWAP")
            algo_id = str(row.get("algoId") or row.get("tpslId") or "").strip()
            state = str(row.get("state") or "").strip().lower()
            if not algo_id or not state:
                raise BloFinDemoPrivateWSError("PRIVATE_WS_ALGO_IDENTITY_INVALID")
            payload_data = {
                "algo_id": algo_id,
                "tpsl_id": str(row.get("tpslId") or ""),
                "client_order_id": str(row.get("clientOrderId") or ""),
                "position_id": str(row.get("positionId") or ""),
                "size": str(row.get("size") or ""),
                "actual_size": str(row.get("actualSize") or ""),
                "side": str(row.get("side") or ""),
                "position_side": str(row.get("positionSide") or ""),
                "margin_mode": str(row.get("marginMode") or ""),
                "state": state,
                "tp_trigger_price": str(row.get("tpTriggerPrice") or ""),
                "tp_trigger_type": str(row.get("tpTriggerPriceType") or ""),
                "sl_trigger_price": str(row.get("slTriggerPrice") or ""),
                "sl_trigger_type": str(row.get("slTriggerPriceType") or ""),
                "trigger_price": str(row.get("triggerPrice") or ""),
                "trigger_type": str(row.get("triggerPriceType") or ""),
                "update_time_ms": update_time,
            }
            identity = [algo_id, update_time, state, payload_data["tp_trigger_price"], payload_data["sl_trigger_price"]]
        events.append({
            "event_id": _event_id(channel, inst_id, identity),
            "inst_id": inst_id,
            "channel": channel,
            "action": action or "update",
            "event_time_ms": update_time,
            "received_at_ms": received,
            "payload": payload_data,
            "source": f"blofin_demo_private_{channel.replace('-', '_')}_ws",
        })
    return {"type": "data", "channel": channel, "events": events}


class BloFinDemoPrivateEventStore:
    """Append-only SQLite/Turso-compatible ledger for account-private Demo observations."""

    def __init__(self, connection: Any) -> None:
        if connection is None or not callable(getattr(connection, "execute", None)):
            raise ValueError("a SQLite-compatible connection is required")
        self._connection = connection
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS blofin_demo_private_events (
                inst_id TEXT NOT NULL,
                channel TEXT NOT NULL,
                event_id TEXT NOT NULL,
                action TEXT NOT NULL,
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
            CREATE TRIGGER IF NOT EXISTS blofin_demo_private_events_no_update
            BEFORE UPDATE ON blofin_demo_private_events
            BEGIN SELECT RAISE(ABORT, 'BloFin Demo private events are append-only'); END
            """
        )
        self._connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS blofin_demo_private_events_no_delete
            BEFORE DELETE ON blofin_demo_private_events
            BEGIN SELECT RAISE(ABORT, 'BloFin Demo private events are append-only'); END
            """
        )
        self._connection.commit()

    def append_many(self, events: list[Mapping[str, Any]]) -> dict[str, int]:
        if not isinstance(events, list):
            raise ValueError("events must be a list")
        normalized = []
        for item in events:
            if not isinstance(item, Mapping):
                raise ValueError("event must be a mapping")
            inst_id = _clean_inst_id(item.get("inst_id"))
            channel = str(item.get("channel") or "")
            if channel not in PRIVATE_CHANNELS:
                raise ValueError("private channel is not allow-listed")
            event_id = str(item.get("event_id") or "").strip()
            action = str(item.get("action") or "update")
            received = item.get("received_at_ms")
            event_time = item.get("event_time_ms")
            source = str(item.get("source") or "")
            payload = item.get("payload")
            if not event_id or not source or not isinstance(payload, Mapping):
                raise ValueError("private event identity, source or payload is invalid")
            if isinstance(received, bool) or not isinstance(received, int) or received <= 0:
                raise ValueError("received_at_ms must be positive")
            if event_time is not None and (
                isinstance(event_time, bool) or not isinstance(event_time, int) or event_time <= 0
            ):
                raise ValueError("event_time_ms must be null or positive")
            normalized.append((
                inst_id, channel, event_id, action, event_time, received,
                json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), allow_nan=False),
                source,
            ))
        if not normalized:
            return {"inserted": 0, "duplicates": 0}
        inserted = 0
        cursor = self._connection.cursor()
        try:
            for row in normalized:
                cursor.execute(
                    """INSERT OR IGNORE INTO blofin_demo_private_events
                    (inst_id, channel, event_id, action, event_time_ms, received_at_ms, payload_json, source)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    row,
                )
                inserted += max(0, int(cursor.rowcount or 0))
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        return {"inserted": inserted, "duplicates": len(normalized) - inserted}

    def read_recent(self, *, inst_id: str, channel: str, limit: int = 100) -> list[dict[str, Any]]:
        if (
            channel not in PRIVATE_CHANNELS
            or isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 1000
        ):
            raise ValueError("channel or limit is invalid")
        cursor = self._connection.execute(
            """SELECT inst_id, channel, event_id, action, event_time_ms, received_at_ms, payload_json, source
               FROM blofin_demo_private_events WHERE inst_id=? AND channel=?
               ORDER BY received_at_ms DESC LIMIT ?""",
            (_clean_inst_id(inst_id), channel, limit),
        )
        names = ("inst_id", "channel", "event_id", "action", "event_time_ms", "received_at_ms", "payload_json", "source")
        rows = []
        for raw in cursor.fetchall():
            row = dict(raw) if isinstance(raw, Mapping) else dict(zip(names, raw))
            row["payload"] = json.loads(row.pop("payload_json"))
            rows.append(row)
        return rows


async def demo_blofin_private_futures_stream(
    client: BloFinDemoReadOnly,
    *,
    inst_ids: tuple[str, ...] = DEFAULT_PRIVATE_SYMBOLS,
    heartbeat_interval_seconds: float = 25.0,
    pong_timeout_seconds: float = 10.0,
) -> AsyncIterable[Any]:
    """Login to Demo private WS then subscribe to allow-listed read-only channels."""
    if not isinstance(client, BloFinDemoReadOnly):
        raise ValueError("a BloFinDemoReadOnly client is required")
    subscription = build_private_subscription(inst_ids)
    if not (
        math.isfinite(float(heartbeat_interval_seconds))
        and math.isfinite(float(pong_timeout_seconds))
        and 1 <= heartbeat_interval_seconds < 30
        and 0 < pong_timeout_seconds < 30
    ):
        raise ValueError("heartbeat and pong timeout must be finite and below 30 seconds")
    try:
        import websockets
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("websockets package is required for private Demo streams") from exc

    expected_acks = {
        (arg["channel"], arg["instId"])
        for arg in subscription["args"]
    }
    async with websockets.connect(
        DEMO_PRIVATE_WS_URL,
        ping_interval=None,
        close_timeout=5,
        open_timeout=15,
        max_queue=2000,
    ) as socket:
        login_args = client.websocket_login_args()
        await socket.send(json.dumps({"op": "login", "args": [login_args]}, separators=(",", ":")))
        login_raw = await asyncio.wait_for(socket.recv(), timeout=10)
        login_payload = json.loads(login_raw.decode("utf-8") if isinstance(login_raw, bytes) else login_raw)
        if (
            not isinstance(login_payload, Mapping)
            or login_payload.get("event") != "login"
            or str(login_payload.get("code")) != "0"
        ):
            code = str(login_payload.get("code") or "UNKNOWN") if isinstance(login_payload, Mapping) else "INVALID"
            raise BloFinDemoPrivateWSError(f"PRIVATE_WS_LOGIN_REJECTED_{code}")
        yield login_payload

        await socket.send(json.dumps(subscription, separators=(",", ":")))
        acknowledged: set[tuple[str, str]] = set()
        while acknowledged != expected_acks:
            raw = await asyncio.wait_for(socket.recv(), timeout=15)
            payload = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
            if not isinstance(payload, Mapping):
                raise BloFinDemoPrivateWSError("INVALID_PRIVATE_SUBSCRIPTION_RESPONSE")
            if payload.get("event") == "error":
                code = str(payload.get("code") or "UNKNOWN")
                raise BloFinDemoPrivateWSError(f"PRIVATE_WS_SUBSCRIBE_REJECTED_{code}")
            if payload.get("event") != "subscribe":
                raise BloFinDemoPrivateWSError("UNEXPECTED_MESSAGE_DURING_PRIVATE_SUBSCRIBE")
            arg = payload.get("arg")
            if not isinstance(arg, Mapping):
                raise BloFinDemoPrivateWSError("INVALID_PRIVATE_SUBSCRIPTION_ACK")
            channel = str(arg.get("channel") or "")
            inst_id = _clean_inst_id(arg.get("instId"))
            identity = (channel, inst_id)
            if identity not in expected_acks or identity in acknowledged:
                raise BloFinDemoPrivateWSError("UNEXPECTED_OR_DUPLICATE_PRIVATE_SUBSCRIPTION_ACK")
            acknowledged.add(identity)
            yield payload

        last_receive = time.monotonic()
        pong_deadline: float | None = None
        while True:
            now = time.monotonic()
            if pong_deadline is not None:
                remaining = pong_deadline - now
                if remaining <= 0:
                    raise TimeoutError("BloFin Demo private WS did not return text pong")
                timeout = min(heartbeat_interval_seconds, remaining)
            else:
                timeout = max(0.1, heartbeat_interval_seconds - (now - last_receive))
            try:
                raw = await asyncio.wait_for(socket.recv(), timeout=timeout)
            except asyncio.TimeoutError:
                now = time.monotonic()
                if pong_deadline is not None:
                    if now >= pong_deadline:
                        raise TimeoutError("BloFin Demo private WS did not return text pong")
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


class BloFinDemoPrivateWebSocketWorker:
    """Authenticated, read-only Demo private WS observer with append-only persistence."""

    def __init__(
        self,
        *,
        client: BloFinDemoReadOnly,
        event_store: BloFinDemoPrivateEventStore,
        inst_ids: tuple[str, ...] = DEFAULT_PRIVATE_SYMBOLS,
        connector: Callable[[], Any] | None = None,
        clock_ms: Callable[[], int] | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
        base_backoff_seconds: float = 1.0,
        max_backoff_seconds: float = 30.0,
        max_attempts: int | None = None,
    ) -> None:
        if not isinstance(client, BloFinDemoReadOnly):
            raise ValueError("a BloFinDemoReadOnly client is required")
        self.inst_ids = tuple(_clean_inst_id(x) for x in inst_ids)
        if not self.inst_ids or len(set(self.inst_ids)) != len(self.inst_ids):
            raise ValueError("instrument list must be non-empty and unique")
        if not isinstance(event_store, BloFinDemoPrivateEventStore):
            raise ValueError("a BloFinDemoPrivateEventStore is required")
        if not (0 < base_backoff_seconds <= max_backoff_seconds):
            raise ValueError("backoff values must be positive and max must be >= base")
        if max_attempts is not None and (
            isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1
        ):
            raise ValueError("max_attempts must be a positive integer or null")
        self.client = client
        self.event_store = event_store
        self.connector = connector or (
            lambda: demo_blofin_private_futures_stream(client, inst_ids=self.inst_ids)
        )
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.sleep = sleep
        self.base_backoff_seconds = float(base_backoff_seconds)
        self.max_backoff_seconds = float(max_backoff_seconds)
        self.max_attempts = max_attempts
        self._attempts = 0
        self._login_ok = False
        self._acks: Counter[str] = Counter()
        self._events: Counter[str] = Counter()
        self._positions_first_observation: set[str] = set()
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
            if self.max_attempts is not None and self._attempts >= self.max_attempts:
                break
            self._attempts += 1
            # A reconnect must earn a fresh login and fresh ACKs.
            self._login_ok = False
            self._acks.clear()
            stream = None
            try:
                stream = self.connector()
                if inspect.isawaitable(stream):
                    stream = await stream
                if not hasattr(stream, "__aiter__"):
                    raise TypeError("private Demo connector must return an async iterable")
                async for raw in stream:
                    if stop_event.is_set():
                        break
                    message = self._decode(raw)
                    received = self.clock_ms()
                    if isinstance(message, Mapping) and message.get("event") == "login":
                        if str(message.get("code")) != "0":
                            raise BloFinDemoPrivateWSError("PRIVATE_WS_LOGIN_REJECTED")
                        self._login_ok = True
                        continue
                    if isinstance(message, Mapping) and message.get("event") == "subscribe":
                        if not self._login_ok:
                            raise BloFinDemoPrivateWSError("SUBSCRIPTION_ACK_BEFORE_LOGIN")
                    elif not (
                        isinstance(message, Mapping)
                        and message.get("event") == "login"
                    ) and not self._login_ok:
                        raise BloFinDemoPrivateWSError("PRIVATE_WS_DATA_BEFORE_LOGIN")
                    normalized = normalize_blofin_private_futures_message(
                        message,
                        expected_symbols=self.inst_ids,
                        received_at_ms=received,
                    )
                    if normalized["type"] == "control":
                        if normalized["event"] == "subscribe":
                            self._acks[f"{normalized['channel']}:{normalized['inst_id']}"] += 1
                        continue
                    events = normalized["events"]
                    if not events:
                        raise BloFinDemoPrivateWSError("EMPTY_PRIVATE_EVENT_BATCH")
                    for index, item in enumerate(events):
                        if item["channel"] == "positions" and item["inst_id"] not in self._positions_first_observation:
                            events[index] = {**item, "action": "initial_or_first_observation"}
                            self._positions_first_observation.add(item["inst_id"])
                    persisted = self.event_store.append_many(events)
                    if not isinstance(persisted, Mapping) or persisted.get("inserted", 0) + persisted.get("duplicates", 0) != len(events):
                        raise BloFinDemoPrivateWSError("PRIVATE_EVENT_PERSISTENCE_NOT_CONFIRMED")
                    self._events[normalized["channel"]] += sum(
                        1 for item in events if item["channel"] == normalized["channel"]
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Do not expose API keys, signatures, passphrases or private payloads in diagnostics.
                self._last_error = type(exc).__name__ + ": private Demo WebSocket read failed"
            finally:
                close = getattr(stream, "aclose", None)
                if callable(close):
                    try:
                        await close()
                    except Exception:
                        pass
            if stop_event.is_set() or (
                self.max_attempts is not None and self._attempts >= self.max_attempts
            ):
                break
            await self.sleep(min(
                self.base_backoff_seconds * (2 ** (self._attempts - 1)),
                self.max_backoff_seconds,
            ))

        expected = len(self.inst_ids) * len(PRIVATE_CHANNELS)
        ack_total = sum(self._acks.values())
        return {
            "status": "STOPPED" if stop_event.is_set() else "MAX_ATTEMPTS_REACHED",
            "environment": "BLOFIN_DEMO_PRIVATE_WS",
            "instrument_count": len(self.inst_ids),
            "login_authenticated": self._login_ok,
            "subscription_ack_count": ack_total,
            "subscription_ack_expected": expected,
            "all_private_subscriptions_acknowledged": ack_total == expected and all(
                self._acks.get(f"{channel}:{symbol}", 0) == 1
                for symbol in self.inst_ids for channel in PRIVATE_CHANNELS
            ),
            "positions_first_observation_symbols": sorted(self._positions_first_observation),
            "channel_event_counts": dict(self._events),
            "orders_initial_snapshot_provided_by_exchange": False,
            "orders_algo_initial_snapshot_provided_by_exchange": False,
            "initial_order_state_requires_rest_reconciliation": True,
            "initial_tpsl_state_requires_rest_reconciliation": True,
            "last_error": self._last_error,
            "socket_status": "DISCONNECTED",
            "api_secret_or_passphrase_logged": False,
            "trade_authorization": "NO_TRADE",
            "orders_submitted": False,
            "orders_cancelled": False,
            "positions_closed": False,
        }
