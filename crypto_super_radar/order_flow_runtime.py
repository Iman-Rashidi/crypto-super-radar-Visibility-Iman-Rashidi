"""Reconnect-safe public Spot WebSocket worker for order-flow ingestion.

This worker manages connection lifecycle, backoff, heartbeat, validation and
durable writes. It does not mark a feed LIVE merely because a socket opens:
VenueOrderFlow still enforces warm-up, freshness and event coverage.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import re
import time
from collections.abc import AsyncIterable, Mapping
from typing import Any, Callable

from .order_flow import VenueOrderFlow
from .order_flow_adapters import ingest_binance_spot_message, ingest_bybit_spot_message

_SYMBOL_RE = re.compile(r"^[A-Z0-9]{4,20}$")


async def public_spot_websocket_stream(provider: str, symbol: str) -> AsyncIterable[Any]:
    """Open a public, read-only Binance or Bybit Spot market-data stream."""
    provider_name = str(provider or "").strip().lower()
    symbol_name = str(symbol or "").strip().upper()
    if provider_name not in {"binance", "bybit"}:
        raise ValueError("provider must be binance or bybit")
    if not _SYMBOL_RE.fullmatch(symbol_name):
        raise ValueError("symbol must be a normalized alphanumeric Spot symbol")
    try:
        import websockets
    except ImportError as exc:  # pragma: no cover - environment-dependent dependency
        raise RuntimeError("websockets package is required for live Spot order-flow streams") from exc

    if provider_name == "binance":
        uri = f"wss://stream.binance.com:9443/ws/{symbol_name.lower()}@aggTrade"
        async with websockets.connect(
            uri, ping_interval=20, ping_timeout=20, close_timeout=5,
            open_timeout=15, max_queue=2_000,
        ) as socket:
            async for message in socket:
                yield message
        return

    uri = "wss://stream.bybit.com/v5/public/spot"
    async with websockets.connect(
        uri, ping_interval=20, ping_timeout=20, close_timeout=5,
        open_timeout=15, max_queue=2_000,
    ) as socket:
        await socket.send(json.dumps({"op": "subscribe", "args": [f"publicTrade.{symbol_name}"]}))
        async for message in socket:
            yield message


class OrderFlowStreamWorker:
    """Consume a provider stream and reconnect without concealing data gaps."""

    def __init__(
        self,
        *,
        provider: str,
        symbol: str,
        tracker: VenueOrderFlow,
        event_store: Any,
        connector: Callable[[], Any] | None = None,
        clock_ms: Callable[[], int] | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
        base_backoff_seconds: float = 1.0,
        max_backoff_seconds: float = 30.0,
        max_attempts: int | None = None,
    ) -> None:
        provider_name = str(provider or "").strip().lower()
        symbol_name = str(symbol or "").strip().upper()
        if provider_name not in {"binance", "bybit"}:
            raise ValueError("provider must be binance or bybit")
        if not _SYMBOL_RE.fullmatch(symbol_name):
            raise ValueError("symbol must be a normalized alphanumeric Spot symbol")
        if not isinstance(tracker, VenueOrderFlow):
            raise ValueError("tracker must be a VenueOrderFlow")
        if event_store is None or not callable(getattr(event_store, "append_many", None)):
            raise ValueError("a durable event store with append_many is required")
        if not (0 < base_backoff_seconds <= max_backoff_seconds):
            raise ValueError("backoff values must be positive and max must be >= base")
        if max_attempts is not None and (
            isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1
        ):
            raise ValueError("max_attempts must be a positive integer or null")

        self.provider = provider_name
        self.symbol = symbol_name
        self.tracker = tracker
        self.event_store = event_store
        self.connector = connector or (lambda: public_spot_websocket_stream(provider_name, symbol_name))
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.sleep = sleep
        self.base_backoff_seconds = float(base_backoff_seconds)
        self.max_backoff_seconds = float(max_backoff_seconds)
        self.max_attempts = max_attempts
        self._counts = {"attempts": 0, "accepted": 0, "duplicates": 0, "rejected": 0, "persisted": 0}
        self._last_error: str | None = None

    @staticmethod
    def _decode(raw: Any) -> Mapping[str, Any] | None:
        if isinstance(raw, Mapping):
            return raw
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        if isinstance(raw, str):
            value = json.loads(raw)
            return value if isinstance(value, Mapping) else None
        return None

    def _is_control_message(self, payload: Mapping[str, Any], received_at_ms: int) -> bool:
        operation = str(payload.get("op") or "").strip().lower()
        if operation in {"pong", "ping", "subscribe", "unsubscribe"}:
            self.tracker.heartbeat(received_at_ms)
            return True
        if payload.get("success") is True and operation:
            self.tracker.heartbeat(received_at_ms)
            return True
        return False

    async def _open_stream(self):
        stream = self.connector()
        if inspect.isawaitable(stream):
            stream = await stream
        if not hasattr(stream, "__aiter__"):
            raise TypeError("connector must return an async iterable of WebSocket messages")
        return stream

    async def run(self, stop_event: Any) -> dict[str, Any]:
        """Run until stopped or max_attempts is exhausted; cancellation propagates."""
        if stop_event is None or not callable(getattr(stop_event, "is_set", None)):
            raise ValueError("stop_event with is_set() is required")

        while not stop_event.is_set():
            if self.max_attempts is not None and self._counts["attempts"] >= self.max_attempts:
                break
            self._counts["attempts"] += 1
            stream = None
            try:
                stream = await self._open_stream()
                self.tracker.connect(self.clock_ms())
                async for raw in stream:
                    if stop_event.is_set():
                        break
                    received_at_ms = self.clock_ms()
                    try:
                        payload = self._decode(raw)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        self.tracker.reject_payload("INVALID_JSON_MESSAGE")
                        self._counts["rejected"] += 1
                        break
                    if payload is None:
                        self.tracker.reject_payload("UNSUPPORTED_WEBSOCKET_PAYLOAD")
                        self._counts["rejected"] += 1
                        break
                    if self._is_control_message(payload, received_at_ms):
                        continue

                    if self.provider == "binance":
                        result = ingest_binance_spot_message(
                            self.tracker, payload, channel_type="spot",
                            received_at_ms=received_at_ms, expected_symbol=self.symbol,
                            event_store=self.event_store,
                        )
                        self._counts["accepted"] += int(bool(result.get("accepted")))
                        self._counts["duplicates"] += int(result.get("reason") == "DUPLICATE_TRADE_ID")
                        self._counts["persisted"] += int(result.get("persisted", 0) or 0)
                        self._counts["rejected"] += int(
                            not result.get("accepted") and result.get("reason") != "DUPLICATE_TRADE_ID"
                        )
                    else:
                        result = ingest_bybit_spot_message(
                            self.tracker, payload, channel_type="spot",
                            received_at_ms=received_at_ms, expected_symbol=self.symbol,
                            event_store=self.event_store,
                        )
                        self._counts["accepted"] += int(result.get("accepted", 0) or 0)
                        self._counts["duplicates"] += int(result.get("duplicates", 0) or 0)
                        self._counts["persisted"] += int(result.get("persisted", 0) or 0)
                        self._counts["rejected"] += int(result.get("rejected", 0) or 0)

                    if self.tracker.snapshot(received_at_ms)["status"] == "FROZEN":
                        self._last_error = str(result.get("detail") or result.get("reason") or "VENUE_FROZEN")
                        break
                else:
                    self._last_error = "STREAM_ENDED"
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {str(exc)[:240]}"
            finally:
                if self.tracker.snapshot(self.clock_ms())["status"] != "DISCONNECTED":
                    self.tracker.disconnect()
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
            "provider": self.provider,
            "symbol": self.symbol,
            "venue": self.tracker.venue,
            **self._counts,
            "last_error": self._last_error,
            "feed_status": self.tracker.snapshot(self.clock_ms())["status"],
            "trade_authorization": "NO_TRADE",
        }
