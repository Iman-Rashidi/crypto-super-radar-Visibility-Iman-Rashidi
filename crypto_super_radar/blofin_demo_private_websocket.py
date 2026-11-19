"""Read-only BloFin Demo private WebSocket bridge with reconnect reconciliation.

This module never submits, amends, cancels, transfers, or withdraws. The only
REST calls it performs are the initial/reconnect reconciliation snapshot.
"""
from __future__ import annotations

import copy
import json
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Mapping


PRIVATE_CHANNELS = ("positions", "orders", "orders-algo", "account")
PRIVATE_WS_REST_KEYS = ("positions", "orders", "algo_orders", "account")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rows_by_key(rows: Any, *keys: str) -> dict[str, dict[str, Any]]:
    if not isinstance(rows, list):
        raise ValueError("BloFin reconciliation rows must be a list.")
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("BloFin reconciliation row is not an object.")
        identifier = next(
            (str(row.get(key) or "").strip() for key in keys if str(row.get(key) or "").strip()),
            "",
        )
        if identifier:
            result[identifier] = dict(row)
    return result


class BloFinDemoPrivateWebSocket:
    """Authenticated Demo-only private stream with REST baseline on every connection."""

    def __init__(
        self,
        reader: Any,
        *,
        reconcile_callback: Callable[[], Mapping[str, Any]] | None = None,
        connect_factory: Callable[..., Any] | None = None,
        event_callback: Callable[[str, Mapping[str, Any]], None] | None = None,
        heartbeat_seconds: float = 20.0,
        pong_timeout_seconds: float = 6.0,
        reconnect_min_seconds: float = 2.0,
        reconnect_max_seconds: float = 30.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if heartbeat_seconds < 5 or heartbeat_seconds >= 30:
            raise ValueError("heartbeat_seconds must be between 5 and 29.")
        if pong_timeout_seconds <= 0 or reconnect_min_seconds <= 0:
            raise ValueError("timeouts and reconnect delays must be positive.")
        if reconnect_max_seconds < reconnect_min_seconds:
            raise ValueError("reconnect_max_seconds must be >= reconnect_min_seconds.")
        self.reader = reader
        self.reconcile_callback = reconcile_callback
        self.connect_factory = connect_factory
        self.event_callback = event_callback
        self.heartbeat_seconds = float(heartbeat_seconds)
        self.pong_timeout_seconds = float(pong_timeout_seconds)
        self.reconnect_min_seconds = float(reconnect_min_seconds)
        self.reconnect_max_seconds = float(reconnect_max_seconds)
        self.clock = clock
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._snapshot: dict[str, Any] = self._empty_snapshot()

    @staticmethod
    def _empty_snapshot() -> dict[str, Any]:
        return {
            "status": "NOT_STARTED",
            "environment": "BLOFIN_DEMO",
            "source": "BLOFIN_DEMO_PRIVATE_WEBSOCKET",
            "connected": False,
            "authenticated": False,
            "reconciled": False,
            "subscribed_channels": [],
            "last_message_at_utc": None,
            "last_message_epoch": None,
            "reconciled_at_utc": None,
            "reconnect_count": 0,
            "events_received": 0,
            "last_error_type": None,
            "channels": {
                channel: {"last_event_at_utc": None, "last_event_epoch": None, "last_action": None}
                for channel in PRIVATE_CHANNELS
            },
            "positions": {},
            "orders": {},
            "algo_orders": {},
            "account": None,
        }

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self.run_forever,
                name="blofin-demo-private-websocket",
                daemon=True,
            )
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        with self._lock:
            thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.0, float(timeout)))

    def is_alive(self) -> bool:
        with self._lock:
            return bool(self._thread is not None and self._thread.is_alive())

    def get_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._snapshot)

    def _update(self, **values: Any) -> None:
        with self._lock:
            self._snapshot.update(values)

    def _mark_message(self) -> None:
        now = float(self.clock())
        self._update(last_message_at_utc=_utc_now(), last_message_epoch=now)

    def _connection_factory(self) -> Callable[..., Any]:
        if self.connect_factory is not None:
            return self.connect_factory
        # Lazy import keeps offline tests and non-WebSocket tooling dependency-light.
        from websockets.sync.client import connect
        return connect

    def _default_reconcile(self) -> Mapping[str, Any]:
        positions = self.reader.get_positions()
        orders = self.reader.get_open_orders()
        algo_orders = self.reader.get_open_tpsl_orders(limit=100)
        account = self.reader.get_futures_balance()
        return {
            "positions": positions,
            "orders": orders,
            "algo_orders": algo_orders,
            "account": account,
        }

    def _seed_reconciliation(self, baseline: Mapping[str, Any]) -> None:
        if not isinstance(baseline, Mapping):
            raise ValueError("BloFin REST reconciliation returned a non-object.")
        positions = _rows_by_key(baseline.get("positions"), "positionId")
        orders = _rows_by_key(baseline.get("orders"), "orderId", "clientOrderId")
        algo_orders = _rows_by_key(baseline.get("algo_orders"), "tpslId", "algoId", "clientOrderId")
        account = baseline.get("account")
        if not isinstance(account, (dict, list)):
            raise ValueError("BloFin account reconciliation is missing or invalid.")
        self._update(
            positions=positions,
            orders=orders,
            algo_orders=algo_orders,
            account=copy.deepcopy(account),
            reconciled=True,
            reconciled_at_utc=_utc_now(),
        )

    def _wait_for_event(
        self,
        websocket: Any,
        *,
        expected_event: str,
        expected_channel: str | None = None,
        timeout_seconds: float = 15.0,
    ) -> None:
        deadline = time.monotonic() + timeout_seconds
        while not self._stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"BloFin WebSocket {expected_event} acknowledgement timed out.")
            raw = websocket.recv(timeout=min(2.0, remaining))
            message = self._decode_message(raw)
            if message is None:
                self._handle_control_message(websocket, raw)
                continue
            event = str(message.get("event") or "").lower()
            if event == "error":
                raise RuntimeError("BloFin WebSocket rejected login/subscription.")
            if event == expected_event:
                if expected_channel is None:
                    if str(message.get("code") or "") != "0":
                        raise RuntimeError("BloFin WebSocket login was not accepted.")
                    return
                arg = message.get("arg") if isinstance(message.get("arg"), Mapping) else {}
                if str(arg.get("channel") or "") == expected_channel:
                    code = str(message.get("code") or "0")
                    if code != "0":
                        raise RuntimeError("BloFin WebSocket subscription was rejected.")
                    with self._lock:
                        subscribed = set(self._snapshot.get("subscribed_channels") or [])
                        subscribed.add(expected_channel)
                        self._snapshot["subscribed_channels"] = sorted(subscribed)
                    return
            self._process_json_message(message)

    def _wait_for_subscriptions(
        self,
        websocket: Any,
        *,
        timeout_seconds: float = 15.0,
    ) -> None:
        """Accept subscription acknowledgements in any order without discarding early ACKs."""
        pending = set(PRIVATE_CHANNELS)
        deadline = time.monotonic() + timeout_seconds
        while pending and not self._stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    "BloFin WebSocket subscription acknowledgements timed out: "
                    + ",".join(sorted(pending))
                )
            raw = websocket.recv(timeout=min(2.0, remaining))
            message = self._decode_message(raw)
            if message is None:
                self._handle_control_message(websocket, raw)
                continue
            event = str(message.get("event") or "").lower()
            if event == "error":
                raise RuntimeError("BloFin WebSocket rejected a private subscription.")
            if event == "subscribe":
                arg = message.get("arg") if isinstance(message.get("arg"), Mapping) else {}
                channel = str(arg.get("channel") or "")
                if channel in pending:
                    if str(message.get("code") or "0") != "0":
                        raise RuntimeError(
                            f"BloFin WebSocket subscription was rejected for {channel}."
                        )
                    pending.remove(channel)
                    with self._lock:
                        subscribed = set(self._snapshot.get("subscribed_channels") or [])
                        subscribed.add(channel)
                        self._snapshot["subscribed_channels"] = sorted(subscribed)
                continue
            # Data can arrive before every subscription ACK. Preserve it rather than
            # dropping it while the remaining channels are being acknowledged.
            self._process_json_message(message)
        if pending and self._stop.is_set():
            raise RuntimeError("BloFin WebSocket stopped before all subscriptions were acknowledged.")

    @staticmethod
    def _decode_message(raw: Any) -> dict[str, Any] | None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="strict")
        if not isinstance(raw, str):
            raise ValueError("BloFin WebSocket message must be text.")
        text = raw.strip()
        if not text or text in {"ping", "pong"}:
            return None
        value = json.loads(text)
        if not isinstance(value, dict):
            raise ValueError("BloFin WebSocket JSON message must be an object.")
        return value

    def _handle_control_message(self, websocket: Any, raw: Any) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        if raw == "ping":
            websocket.send("pong")
        elif raw == "pong":
            pass
        self._mark_message()

    def _merge_channel_rows(self, channel: str, rows: Any, action: str) -> None:
        if isinstance(rows, Mapping):
            normalized = [dict(rows)]
        elif isinstance(rows, list):
            normalized = [dict(row) for row in rows if isinstance(row, Mapping)]
        else:
            raise ValueError("BloFin WebSocket channel data is not a list/object.")
        key_map = {
            "positions": ("positionId",),
            "orders": ("orderId", "clientOrderId"),
            "orders-algo": ("tpslId", "algoId", "clientOrderId"),
        }
        target_name = {
            "positions": "positions",
            "orders": "orders",
            "orders-algo": "algo_orders",
        }.get(channel)
        now = float(self.clock())
        with self._lock:
            if channel == "account":
                self._snapshot["account"] = copy.deepcopy(rows)
            elif target_name:
                target = dict(self._snapshot.get(target_name) or {})
                if action == "snapshot":
                    target = {}
                for row in normalized:
                    identifier = next(
                        (str(row.get(key) or "").strip() for key in key_map[channel]
                         if str(row.get(key) or "").strip()),
                        "",
                    )
                    if identifier:
                        # Zero-sized position rows are retained as close events for audit.
                        target[identifier] = {**target.get(identifier, {}), **row}
                self._snapshot[target_name] = target
            channel_state = dict(self._snapshot["channels"].get(channel) or {})
            channel_state.update({
                "last_event_at_utc": _utc_now(),
                "last_event_epoch": now,
                "last_action": action or "update",
            })
            self._snapshot["channels"][channel] = channel_state
            self._snapshot["events_received"] = int(self._snapshot.get("events_received") or 0) + 1
            self._snapshot["last_message_at_utc"] = _utc_now()
            self._snapshot["last_message_epoch"] = now

    def _process_json_message(self, message: Mapping[str, Any]) -> None:
        channel_info = message.get("arg")
        channel = str(channel_info.get("channel") or "") if isinstance(channel_info, Mapping) else ""
        if channel not in PRIVATE_CHANNELS:
            self._mark_message()
            return
        self._merge_channel_rows(
            channel,
            message.get("data", []),
            str(message.get("action") or "update").lower(),
        )
        if self.event_callback is not None:
            try:
                self.event_callback(channel, copy.deepcopy(dict(message)))
            except Exception as exc:
                self._update(last_event_callback_error_type=type(exc).__name__)

    def _process_raw(self, websocket: Any, raw: Any) -> None:
        message = self._decode_message(raw)
        if message is None:
            self._handle_control_message(websocket, raw)
            return
        if str(message.get("event") or "").lower() == "error":
            raise RuntimeError("BloFin WebSocket returned an error event.")
        self._process_json_message(message)

    def _heartbeat(self, websocket: Any) -> None:
        websocket.send("ping")
        deadline = time.monotonic() + self.pong_timeout_seconds
        while not self._stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("BloFin WebSocket pong was not received.")
            try:
                raw = websocket.recv(timeout=min(1.0, remaining))
            except TimeoutError:
                continue
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            if raw == "pong":
                self._mark_message()
                return
            self._process_raw(websocket, raw)
        raise RuntimeError("BloFin WebSocket stopped during heartbeat.")

    def _run_session(self) -> None:
        connector = self._connection_factory()
        private_url = self.reader.websocket_urls()["private"]
        with connector(
            private_url,
            open_timeout=10,
            ping_interval=None,
            ping_timeout=None,
            close_timeout=5,
            max_size=1_048_576,
        ) as websocket:
            self._update(
                status="AUTHENTICATING",
                connected=True,
                authenticated=False,
                reconciled=False,
                subscribed_channels=[],
                last_error_type=None,
            )
            login = {"op": "login", "args": [self.reader.websocket_login_args()]}
            websocket.send(json.dumps(login, separators=(",", ":")))
            self._wait_for_event(websocket, expected_event="login")
            self._update(authenticated=True, status="SUBSCRIBING")
            subscribe = {
                "op": "subscribe",
                "args": [{"channel": channel} for channel in PRIVATE_CHANNELS],
            }
            websocket.send(json.dumps(subscribe, separators=(",", ":")))
            self._wait_for_subscriptions(websocket)
            baseline = (
                self.reconcile_callback() if self.reconcile_callback is not None
                else self._default_reconcile()
            )
            self._seed_reconciliation(baseline)
            self._update(status="READY", connected=True, authenticated=True)
            while not self._stop.is_set():
                try:
                    raw = websocket.recv(timeout=self.heartbeat_seconds)
                except TimeoutError:
                    self._heartbeat(websocket)
                    continue
                self._process_raw(websocket, raw)

    def run_forever(self) -> None:
        delay = self.reconnect_min_seconds
        while not self._stop.is_set():
            try:
                with self._lock:
                    reconnect_count = int(self._snapshot.get("reconnect_count") or 0)
                if reconnect_count:
                    self._update(status="RECONNECTING")
                self._run_session()
                delay = self.reconnect_min_seconds
            except Exception as exc:
                with self._lock:
                    reconnect_count = int(self._snapshot.get("reconnect_count") or 0) + 1
                    self._snapshot.update({
                        "status": "DISCONNECTED",
                        "connected": False,
                        "authenticated": False,
                        "reconciled": False,
                        "subscribed_channels": [],
                        "reconnect_count": reconnect_count,
                        "last_error_type": type(exc).__name__,
                    })
            if self._stop.is_set():
                break
            self._stop.wait(delay)
            delay = min(self.reconnect_max_seconds, max(self.reconnect_min_seconds, delay * 2))
