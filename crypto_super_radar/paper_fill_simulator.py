"""Deterministic, offline PAPER_ONLY fill simulation.

No network, exchange credentials, real order submission, sleep, or randomness is
used. Missing liquidity and stale observed quotes fail closed.
"""
from __future__ import annotations

import math
import time
from typing import Any, Sequence

from .execution_costs import calculate_fill_costs
from .paper_execution import PaperExecutionEventLedger


class PaperFillSimulationError(ValueError):
    """The supplied quote, liquidity, or fill schedule is invalid."""


def _finite(value: Any, field: str, *, min_value: float = 0.0) -> float:
    if isinstance(value, bool) or value is None:
        raise PaperFillSimulationError(f"{field} is required and must be finite")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise PaperFillSimulationError(f"{field} is required and must be finite") from exc
    if not math.isfinite(number) or number < min_value:
        raise PaperFillSimulationError(f"{field} must be finite and >= {min_value}")
    return number


def _schedule(values: Sequence[float]) -> tuple[float, ...]:
    if isinstance(values, (str, bytes)) or not values:
        raise PaperFillSimulationError("fill_fractions must contain at least one fraction")
    if len(values) > 20:
        raise PaperFillSimulationError("fill_fractions cannot contain more than 20 slices")
    result = tuple(_finite(v, f"fill_fractions[{i}]") for i, v in enumerate(values))
    if any(v <= 0 for v in result):
        raise PaperFillSimulationError("every fill fraction must be positive")
    if not math.isclose(sum(result), 1.0, rel_tol=1e-9, abs_tol=1e-9):
        raise PaperFillSimulationError("fill_fractions must sum to 1.0")
    return result


class PaperFillSimulator:
    """Simulate deterministic top-of-book fills and optionally persist events.

        When an event ledger is supplied, the caller owns the surrounding DB
        transaction. Wrap the whole simulation in the connection context so an
        incomplete event sequence cannot be committed independently of its projection.
        """

    def __init__(self, event_ledger: PaperExecutionEventLedger | None = None):
        self.event_ledger = event_ledger

    def simulate_order(
        self,
        *,
        client_order_id: str,
        asset: str,
        side: str,
        requested_quantity: Any,
        bid: Any,
        ask: Any,
        available_liquidity_quantity: Any,
        max_participation_rate: Any = 1.0,
        fee_bps: Any = 8.0,
        adverse_impact_bps: Any = 0.0,
        simulated_latency_ms: int = 0,
        fill_fractions: Sequence[float] = (1.0,),
        quote_quality: str = "OBSERVED",
        quote_source: str,
        quote_timestamp_ms: int | None = None,
        now_ms: int | None = None,
        max_quote_age_ms: int = 5_000,
        cancel_unfilled_remainder: bool = False,
        reason: str = "PAPER_SIMULATION",
    ) -> dict[str, Any]:
        """Simulate fills from explicit base-asset liquidity, with no live adapter."""
        order_id = str(client_order_id or "").strip()
        asset_value = str(asset or "").strip()
        side_value = str(side or "").strip().upper()
        source_value = str(quote_source or "").strip()
        quality = str(quote_quality or "").strip().upper()
        reason_value = str(reason or "").strip()
        if not order_id or not asset_value or not source_value:
            raise PaperFillSimulationError("client_order_id, asset and quote_source are required")
        if side_value not in {"BUY", "SELL"}:
            raise PaperFillSimulationError("side must be BUY or SELL")
        if quality not in {"OBSERVED", "MODELLED"}:
            raise PaperFillSimulationError("quote_quality must be OBSERVED or MODELLED")
        if not isinstance(cancel_unfilled_remainder, bool):
            raise PaperFillSimulationError("cancel_unfilled_remainder must be boolean")

        qty = _finite(requested_quantity, "requested_quantity")
        if qty <= 0:
            raise PaperFillSimulationError("requested_quantity must be positive")
        best_bid = _finite(bid, "bid")
        best_ask = _finite(ask, "ask")
        if best_bid <= 0 or best_ask < best_bid:
            raise PaperFillSimulationError("quote must have positive bid and ask >= bid")
        liquidity = _finite(available_liquidity_quantity, "available_liquidity_quantity")
        participation = _finite(max_participation_rate, "max_participation_rate")
        if not 0 < participation <= 1:
            raise PaperFillSimulationError("max_participation_rate must be in (0, 1]")
        fee_rate_bps = _finite(fee_bps, "fee_bps")
        impact_bps = _finite(adverse_impact_bps, "adverse_impact_bps")
        latency = int(simulated_latency_ms)
        if isinstance(simulated_latency_ms, bool) or latency != simulated_latency_ms or latency < 0:
            raise PaperFillSimulationError("simulated_latency_ms must be a non-negative integer")
        if latency > 300_000:
            raise PaperFillSimulationError("simulated_latency_ms cannot exceed 300000")
        if isinstance(max_quote_age_ms, bool) or not isinstance(max_quote_age_ms, int) or max_quote_age_ms < 0:
            raise PaperFillSimulationError("max_quote_age_ms must be a non-negative integer")

        current_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
        if quality == "OBSERVED":
            if quote_timestamp_ms is None:
                raise PaperFillSimulationError("observed quote requires quote_timestamp_ms")
            quote_ms = int(quote_timestamp_ms)
            quote_age_ms = current_ms - quote_ms
            if quote_age_ms < -2_000 or quote_age_ms > max_quote_age_ms:
                raise PaperFillSimulationError("observed quote is stale or from the future")
        else:
            quote_ms = None if quote_timestamp_ms is None else int(quote_timestamp_ms)
            quote_age_ms = None if quote_ms is None else current_ms - quote_ms
            if quote_age_ms is not None and (quote_age_ms < -2_000 or quote_age_ms > max_quote_age_ms):
                raise PaperFillSimulationError("modelled quote timestamp is outside the accepted window")

        fractions = _schedule(fill_fractions)
        fillable = min(qty, liquidity * participation)
        tolerance = max(1e-12, qty * 1e-12)
        if fillable >= qty - tolerance:
            fillable = qty
        start_ms = current_ms
        events: list[dict[str, Any]] = [
            {"status": "CREATED", "occurred_at_ms": start_ms},
            {"status": "VALIDATED", "occurred_at_ms": start_ms},
            {"status": "SUBMITTED", "occurred_at_ms": start_ms},
            {"status": "ACCEPTED", "occurred_at_ms": start_ms},
        ]
        if self.event_ledger is not None:
            self.event_ledger.append_event(
                client_order_id=order_id, idempotency_key=f"{order_id}:created",
                asset=asset_value, side=side_value, status="CREATED", quantity=qty,
                source="PAPER_FILL_SIMULATOR", reason=reason_value, occurred_at=start_ms // 1000,
            )
            self.event_ledger.append_event(
                client_order_id=order_id, idempotency_key=f"{order_id}:validated",
                asset=asset_value, side=side_value, status="VALIDATED", quantity=qty,
                source="PAPER_FILL_SIMULATOR", reason=reason_value, occurred_at=start_ms // 1000,
            )
            self.event_ledger.append_event(
                client_order_id=order_id, idempotency_key=f"{order_id}:submitted",
                asset=asset_value, side=side_value, status="SUBMITTED", quantity=qty,
                source="PAPER_FILL_SIMULATOR", reason=reason_value, occurred_at=start_ms // 1000,
            )
            self.event_ledger.append_event(
                client_order_id=order_id, idempotency_key=f"{order_id}:accepted",
                asset=asset_value, side=side_value, status="ACCEPTED", quantity=qty,
                source="PAPER_FILL_SIMULATOR", reason=reason_value, occurred_at=start_ms // 1000,
            )

        fills: list[dict[str, Any]] = []
        cumulative_qty = 0.0
        cumulative_notional = 0.0
        cumulative_fee = 0.0
        spread_total = 0.0
        slippage_total = 0.0
        remaining = fillable
        touch = best_ask if side_value == "BUY" else best_bid
        multiplier = 1.0 + impact_bps / 10_000.0 if side_value == "BUY" else 1.0 - impact_bps / 10_000.0
        if multiplier <= 0:
            raise PaperFillSimulationError("adverse_impact_bps makes the simulated fill price non-positive")

        if fillable > 0:
            for index, fraction in enumerate(fractions):
                slice_qty = remaining if index == len(fractions) - 1 else fillable * fraction
                if index != len(fractions) - 1:
                    remaining -= slice_qty
                if slice_qty <= 0:
                    continue
                fill_price = touch * multiplier
                fee = slice_qty * fill_price * fee_rate_bps / 10_000.0
                costs = calculate_fill_costs(
                    side=side_value, quantity=slice_qty, bid=best_bid, ask=best_ask,
                    fill_price=fill_price, fee_paid_usd=fee,
                    funding_applicable=False, quote_quality=quality,
                )
                cumulative_qty += slice_qty
                cumulative_notional += slice_qty * fill_price
                cumulative_fee += fee
                spread_total += float(costs["spread_cost_usd"])
                slippage_total += float(costs["slippage_cost_usd"])
                full = math.isclose(cumulative_qty, qty, rel_tol=1e-9, abs_tol=tolerance)
                if full:
                    cumulative_qty = qty
                average_price = cumulative_notional / cumulative_qty
                event_ms = start_ms + int(latency * (index + 1) / len(fractions))
                status = "FILLED" if full else "PARTIALLY_FILLED"
                fill = {
                    "slice": index + 1,
                    "quantity": slice_qty,
                    "cumulative_filled_quantity": cumulative_qty,
                    "average_fill_price": average_price,
                    "fee_paid_usd": fee,
                    "cumulative_fee_usd": cumulative_fee,
                    "spread_cost_usd": float(costs["spread_cost_usd"]),
                    "slippage_cost_usd": float(costs["slippage_cost_usd"]),
                    "quote_quality": quality,
                    "quote_source": source_value,
                    "occurred_at_ms": event_ms,
                }
                fills.append(fill)
                events.append({
                    "status": status, "filled_quantity": cumulative_qty,
                    "average_fill_price": average_price, "occurred_at_ms": event_ms,
                })
                if self.event_ledger is not None:
                    self.event_ledger.append_event(
                        client_order_id=order_id,
                        idempotency_key=f"{order_id}:fill:{index + 1}",
                        asset=asset_value, side=side_value, status=status, quantity=qty,
                        filled_quantity=cumulative_qty, fill_price=average_price,
                        fee_paid_usd=cumulative_fee, source=f"PAPER_SIMULATOR:{source_value}",
                        reason=reason_value, occurred_at=event_ms // 1000,
                    )

        unfilled = max(0.0, qty - cumulative_qty)
        if unfilled > tolerance and cancel_unfilled_remainder:
            cancel_ms = fills[-1]["occurred_at_ms"] if fills else start_ms
            events.append({
                "status": "CANCELED", "filled_quantity": cumulative_qty,
                "occurred_at_ms": cancel_ms,
            })
            if self.event_ledger is not None:
                self.event_ledger.append_event(
                    client_order_id=order_id, idempotency_key=f"{order_id}:canceled",
                    asset=asset_value, side=side_value, status="CANCELED", quantity=qty,
                    filled_quantity=cumulative_qty, source="PAPER_FILL_SIMULATOR",
                    reason="UNFILLED_REMAINDER_CANCELED" if cumulative_qty else "NO_AVAILABLE_LIQUIDITY",
                    occurred_at=cancel_ms // 1000,
                )

        if unfilled <= tolerance:
            final_status = "FILLED"
        elif cancel_unfilled_remainder:
            final_status = "CANCELED"
        elif cumulative_qty > 0:
            final_status = "PARTIALLY_FILLED"
        else:
            final_status = "ACCEPTED"

        return {
            "mode": "PAPER_ONLY", "orders_submitted": False, "transactions_signed": False,
            "client_order_id": order_id, "asset": asset_value, "side": side_value,
            "status": final_status, "requested_quantity": qty,
            "filled_quantity": cumulative_qty, "remaining_quantity": unfilled,
            "fill_count": len(fills), "fills": fills, "simulated_events": events,
            "quote": {
                "bid": best_bid, "ask": best_ask, "quality": quality, "source": source_value,
                "timestamp_ms": quote_ms, "age_ms": quote_age_ms,
            },
            "execution_costs": {
                "spread_cost_usd": round(spread_total, 10),
                "adverse_slippage_cost_usd": round(slippage_total, 10),
                "fee_cost_usd": round(cumulative_fee, 10),
                "funding_cost_usd": 0.0, "funding_status": "NOT_APPLICABLE",
                "status": "COMPLETE" if fills else "NO_FILL",
            },
            "simulated_latency_ms": latency, "latency_is_wall_clock_delay": False,
            "liquidity": {
                "available_quantity": liquidity, "max_participation_rate": participation,
                "fillable_quantity": fillable, "participation_cap_applied": fillable < qty,
            },
        }
