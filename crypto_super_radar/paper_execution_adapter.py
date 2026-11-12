"""Deterministic, network-free partial-fill simulator for PAPER_ONLY orders.

This adapter is deliberately injected with a PaperExecutionEventLedger and
explicit book snapshots. It never contacts an exchange or submits real orders.
"""
from __future__ import annotations

import math
import time
from typing import Any

from .paper_execution import IdempotencyConflict, PaperExecutionEventLedger


def _positive(value: Any, name: str, *, allow_zero: bool = False) -> float:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{name} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(number) or (number < 0 if allow_zero else number <= 0):
        raise ValueError(f"{name} is outside the allowed range")
    return number


class PaperExecutionAdapter:
    """Submit internal paper intents and consume supplied liquidity snapshots."""

    def __init__(self, ledger: PaperExecutionEventLedger):
        self.ledger = ledger

    def submit_order(
        self, *, client_order_id: str, asset: str, side: str,
        quantity: Any, occurred_at: int | None = None,
    ) -> dict[str, Any]:
        order_id = str(client_order_id or "").strip()
        asset_value = str(asset or "").strip()
        side_value = str(side or "").strip().upper()
        qty = _positive(quantity, "quantity")
        if not order_id or not asset_value or side_value not in {"BUY", "SELL"}:
            raise ValueError("client_order_id, asset and BUY/SELL side are required")
        existing = self.ledger.order_state(order_id)
        if existing is not None:
            if (
                existing["asset"] != asset_value
                or existing["side"] != side_value
                or not math.isclose(float(existing["quantity"]), qty, rel_tol=1e-9, abs_tol=1e-12)
            ):
                raise ValueError("client_order_id already belongs to a different order intent")
            return existing
        timestamp = int(time.time()) if occurred_at is None else int(occurred_at)
        for status in ("CREATED", "VALIDATED", "SUBMITTED", "ACCEPTED"):
            self.ledger.append_event(
                client_order_id=order_id,
                idempotency_key=f"{order_id}:{status.lower()}",
                asset=asset_value, side=side_value, status=status,
                quantity=qty, source="PAPER_EXECUTION_ADAPTER",
                reason="INTERNAL_PAPER_ORDER_ONLY", occurred_at=timestamp,
            )
        return self.ledger.order_state(order_id) or {}

    def simulate_fill(
        self, *, client_order_id: str, snapshot_id: str,
        best_bid: Any, best_ask: Any, available_liquidity_qty: Any,
        max_participation_pct: Any = 0.10,
        fee_rate: Any = 0.0008, slippage_bps: Any = 10.0,
        quote_age_ms: Any = 0, max_quote_age_ms: int = 5_000,
        occurred_at: int | None = None,
    ) -> dict[str, Any]:
        order_id = str(client_order_id or "").strip()
        snapshot = str(snapshot_id or "").strip()
        if not order_id or not snapshot:
            raise ValueError("client_order_id and snapshot_id are required")
        state = self.ledger.order_state(order_id)
        if state is None:
            return {"status": "UNKNOWN", "reason": "paper order does not exist"}

        bid = _positive(best_bid, "best_bid")
        ask = _positive(best_ask, "best_ask")
        if ask < bid:
            raise ValueError("best_ask must be greater than or equal to best_bid")
        liquidity = _positive(available_liquidity_qty, "available_liquidity_qty", allow_zero=True)
        participation = _positive(max_participation_pct, "max_participation_pct")
        if participation > 1.0:
            raise ValueError("max_participation_pct cannot exceed 1.0")
        fees = _positive(fee_rate, "fee_rate", allow_zero=True)
        slip = _positive(slippage_bps, "slippage_bps", allow_zero=True)
        age = _positive(quote_age_ms, "quote_age_ms", allow_zero=True)
        if slip >= 10_000:
            raise ValueError("slippage_bps must be below 10000")

        idempotency_key = f"{order_id}:book:{snapshot}"
        history = self.ledger.recent_events(1000, client_order_id=order_id)
        prior_index = next(
            (i for i, item in enumerate(history) if item["idempotency_key"] == idempotency_key),
            None,
        )
        prior_event = history[prior_index] if prior_index is not None else None
        previous_event = (
            history[prior_index + 1]
            if prior_index is not None and prior_index + 1 < len(history) else None
        )
        previous_filled = float(previous_event["filled_quantity"]) if previous_event else 0.0
        requested = float(prior_event["quantity"] if prior_event else state["quantity"])
        already_filled = previous_filled if prior_event else float(state["filled_quantity"])
        remaining = max(0.0, requested - already_filled)
        if remaining <= 1e-12:
            if prior_event:
                raise IdempotencyConflict("snapshot id does not describe a new fill")
            return {"status": "BLOCKED", "reason": "order quantity is already fully filled"}
        fill_quantity = min(remaining, liquidity * participation)
        if fill_quantity <= 1e-12:
            if prior_event:
                raise IdempotencyConflict("snapshot id was reused with zero available fill quantity")
            if age > max_quote_age_ms:
                return {"status": "UNKNOWN", "reason": "quote snapshot is stale; no fill was simulated"}
            if state["status"] not in {"ACCEPTED", "PARTIALLY_FILLED"}:
                return {"status": "BLOCKED", "reason": "order is not in a fillable state"}
            return {"status": "NO_FILL", "filled_quantity": already_filled, "remaining_quantity": remaining}
        if age > max_quote_age_ms and not prior_event:
            return {"status": "UNKNOWN", "reason": "quote snapshot is stale; no fill was simulated"}
        if not prior_event and state["status"] not in {"ACCEPTED", "PARTIALLY_FILLED"}:
            return {"status": "BLOCKED", "reason": "order is not in a fillable state"}

        side = prior_event["side"] if prior_event else state["side"]
        asset = prior_event["asset"] if prior_event else state["asset"]
        raw_price = ask if side == "BUY" else bid
        fill_price = raw_price * (1.0 + slip / 10_000.0) if side == "BUY" else raw_price * (1.0 - slip / 10_000.0)
        if fill_price <= 0 or not math.isfinite(fill_price):
            raise ValueError("simulated fill price is invalid")
        fee_paid = fill_quantity * fill_price * fees
        previous_fee = (
            previous_event.get("fee_paid_usd")
            if prior_event and previous_event is not None
            else state.get("fee_paid_usd") if not prior_event else None
        )
        cumulative_fee_paid = (
            float(previous_fee) if previous_fee is not None else 0.0
        ) + fee_paid
        cumulative = min(requested, already_filled + fill_quantity)
        status = "FILLED" if math.isclose(cumulative, requested, rel_tol=1e-9, abs_tol=1e-12) else "PARTIALLY_FILLED"
        event = self.ledger.append_event(
            client_order_id=order_id,
            idempotency_key=idempotency_key,
            asset=asset, side=side, status=status,
            quantity=requested, filled_quantity=cumulative,
            fill_price=fill_price, fee_paid_usd=cumulative_fee_paid,
            source="PAPER_ORDER_BOOK_SNAPSHOT",
            reason="SIMULATED_PARTIAL_FILL" if status == "PARTIALLY_FILLED" else "SIMULATED_COMPLETE_FILL",
            occurred_at=(prior_event["occurred_at"] if prior_event and occurred_at is None else occurred_at),
        )
        if not event.get("inserted", False):
            return {
                "status": "DUPLICATE_SNAPSHOT", "client_order_id": order_id,
                "event_id": event["event_id"], "new_fill_quantity": 0.0,
            }
        return {
            "status": status, "client_order_id": order_id,
            "event_id": event["event_id"], "new_fill_quantity": fill_quantity,
            "filled_quantity": cumulative, "remaining_quantity": max(0.0, requested - cumulative),
            "fill_price": fill_price, "fee_paid_usd": fee_paid,
            "cumulative_fee_paid_usd": cumulative_fee_paid,
            "source": "PAPER_ORDER_BOOK_SNAPSHOT", "orders_submitted": False,
        }
