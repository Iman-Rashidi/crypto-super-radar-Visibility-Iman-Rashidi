"""Provider-specific WebSocket message normalization for per-venue CVD.

This module intentionally does not open sockets, create background tasks, or
claim feed health. The caller must manage the WebSocket lifecycle and call
VenueOrderFlow.connect()/disconnect(). Only real stream messages are ingested.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from .order_flow import VenueOrderFlow


class OrderFlowAdapterError(ValueError):
    """A provider message cannot be safely normalized into a spot trade."""


def _positive_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or value is None:
        raise OrderFlowAdapterError(f"INVALID_{field.upper()}")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise OrderFlowAdapterError(f"INVALID_{field.upper()}") from exc
    if not math.isfinite(number) or number <= 0:
        raise OrderFlowAdapterError(f"INVALID_{field.upper()}")
    return number


def _timestamp_ms(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise OrderFlowAdapterError(f"INVALID_{field.upper()}")
    if isinstance(value, int):
        stamp = value
    elif isinstance(value, str) and value.isdigit():
        stamp = int(value)
    else:
        raise OrderFlowAdapterError(f"INVALID_{field.upper()}")
    if stamp <= 0:
        raise OrderFlowAdapterError(f"INVALID_{field.upper()}")
    return stamp


def _identifier(value: Any, field: str) -> str:
    if isinstance(value, bool) or value is None:
        raise OrderFlowAdapterError(f"MISSING_{field.upper()}")
    identifier = str(value).strip()
    if not identifier:
        raise OrderFlowAdapterError(f"MISSING_{field.upper()}")
    return identifier


def _symbol(value: Any, expected_symbol: str | None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OrderFlowAdapterError("MISSING_SYMBOL")
    symbol = value.strip().upper()
    if expected_symbol is not None and symbol != expected_symbol.strip().upper():
        raise OrderFlowAdapterError("SYMBOL_MISMATCH")
    return symbol


def normalize_binance_spot_agg_trade(
    payload: Mapping[str, Any],
    *,
    received_at_ms: int,
    expected_symbol: str | None = None,
) -> dict[str, Any]:
    """Normalize one Binance Spot <symbol>@aggTrade stream event.

    Binance's m=true means the buyer was maker; therefore the seller was the
    taker/aggressor. The exchange trade timestamp T, not local receive time E,
    determines event-time CVD membership.
    """
    if not isinstance(payload, Mapping) or payload.get("e") != "aggTrade":
        raise OrderFlowAdapterError("UNEXPECTED_BINANCE_EVENT")
    _symbol(payload.get("s"), expected_symbol)
    maker_is_buyer = payload.get("m")
    if not isinstance(maker_is_buyer, bool):
        raise OrderFlowAdapterError("INVALID_BUYER_MAKER_FLAG")
    return {
        "trade_id": _identifier(payload.get("a"), "trade_id"),
        "event_time_ms": _timestamp_ms(payload.get("T"), "event_timestamp"),
        "received_at_ms": _timestamp_ms(received_at_ms, "receive_timestamp"),
        "price": _positive_number(payload.get("p"), "price"),
        "base_quantity": _positive_number(payload.get("q"), "base_quantity"),
        "aggressor_side": "SELL" if maker_is_buyer else "BUY",
        "source": "binance_spot_aggTrade",
    }


def normalize_bybit_spot_public_trade_message(
    payload: Mapping[str, Any],
    *,
    received_at_ms: int,
    expected_symbol: str,
) -> list[dict[str, Any]]:
    """Normalize all trades in one Bybit V5 publicTrade.{symbol} message."""
    if not isinstance(payload, Mapping):
        raise OrderFlowAdapterError("INVALID_BYBIT_MESSAGE")
    symbol = _symbol(expected_symbol, expected_symbol)
    if payload.get("topic") != f"publicTrade.{symbol}":
        raise OrderFlowAdapterError("UNEXPECTED_BYBIT_TOPIC")
    if payload.get("type") != "snapshot":
        raise OrderFlowAdapterError("UNEXPECTED_BYBIT_MESSAGE_TYPE")
    rows = payload.get("data")
    if not isinstance(rows, list) or not rows:
        raise OrderFlowAdapterError("EMPTY_OR_INVALID_BYBIT_TRADE_BATCH")

    received = _timestamp_ms(received_at_ms, "receive_timestamp")
    normalized: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise OrderFlowAdapterError("INVALID_BYBIT_TRADE_ROW")
        _symbol(row.get("s"), symbol)
        side = row.get("S")
        if not isinstance(side, str) or side.strip().lower() not in {"buy", "sell"}:
            raise OrderFlowAdapterError("INVALID_TAKER_SIDE")
        normalized.append({
            "trade_id": _identifier(row.get("i"), "trade_id"),
            "event_time_ms": _timestamp_ms(row.get("T"), "event_timestamp"),
            "received_at_ms": received,
            "price": _positive_number(row.get("p"), "price"),
            # Bybit spot v is base-asset trade size, not quote notional.
            "base_quantity": _positive_number(row.get("v"), "base_quantity"),
            "aggressor_side": side.strip().upper(),
            "source": "bybit_spot_publicTrade",
        })
    return normalized


def _freeze_adapter_error(venue: VenueOrderFlow, error: OrderFlowAdapterError) -> dict[str, Any]:
    return venue.reject_payload(error.args[0] if error.args else "UNSPECIFIED_ADAPTER_ERROR")


def ingest_binance_spot_message(
    venue: VenueOrderFlow,
    payload: Mapping[str, Any],
    *,
    received_at_ms: int,
    expected_symbol: str,
) -> dict[str, Any]:
    """Normalize and ingest one raw or combined Binance Spot aggTrade message."""
    try:
        message: Any = payload
        if isinstance(payload, Mapping) and "data" in payload:
            stream = payload.get("stream")
            if stream != f"{expected_symbol.lower()}@aggTrade":
                raise OrderFlowAdapterError("UNEXPECTED_BINANCE_STREAM")
            message = payload.get("data")
        event = normalize_binance_spot_agg_trade(
            message,
            received_at_ms=received_at_ms,
            expected_symbol=expected_symbol,
        )
    except OrderFlowAdapterError as exc:
        return _freeze_adapter_error(venue, exc)
    return venue.ingest_trade(**event)


def ingest_bybit_spot_message(
    venue: VenueOrderFlow,
    payload: Mapping[str, Any],
    *,
    received_at_ms: int,
    expected_symbol: str,
) -> dict[str, Any]:
    """Normalize and ingest a Bybit Spot publicTrade message (one or many trades)."""
    try:
        events = normalize_bybit_spot_public_trade_message(
            payload,
            received_at_ms=received_at_ms,
            expected_symbol=expected_symbol,
        )
    except OrderFlowAdapterError as exc:
        return _freeze_adapter_error(venue, exc)

    results = [
        venue.ingest_trade(**event)
        for event in events
    ]
    return {
        "accepted": sum(bool(item.get("accepted")) for item in results),
        "duplicates": sum(item.get("reason") == "DUPLICATE_TRADE_ID" for item in results),
        "rejected": sum(not item.get("accepted") and item.get("reason") != "DUPLICATE_TRADE_ID" for item in results),
        "results": results,
    }
