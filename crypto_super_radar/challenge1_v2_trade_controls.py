"""Pure, fail-closed policy primitives for Challenge 1 V2 Demo position control.

This module deliberately does not submit exchange orders. Exchange-specific order
amend/close APIs and durable position reconciliation must be wired by the runtime
adapter before these plans can authorize trading.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_DOWN, ROUND_CEILING, ROUND_FLOOR
from typing import Any, Mapping, Sequence


class V2PositionControlBlocked(ValueError):
    """A V2 position-control invariant cannot be proven."""


def v2_entry_capacity(
    open_positions: Sequence[Mapping[str, Any]],
    pending_entries: Sequence[Mapping[str, Any]],
    *,
    candidate_symbol: str,
    max_concurrent_positions: int = 4,
) -> tuple[bool, str]:
    """Enforce account-wide open+pending capacity and one active trade per symbol."""
    if isinstance(max_concurrent_positions, bool) or not 1 <= max_concurrent_positions <= 4:
        raise V2PositionControlBlocked("V2 capacity must be between one and four.")
    symbol = str(candidate_symbol or "").strip().upper()
    if not symbol:
        raise V2PositionControlBlocked("Candidate symbol is required.")
    active = [*open_positions, *pending_entries]
    for item in active:
        if not isinstance(item, Mapping):
            raise V2PositionControlBlocked("Position snapshot is malformed.")
        existing = str(item.get("instId") or item.get("instrument_id") or "").strip().upper()
        if existing == symbol:
            return False, "SYMBOL_ALREADY_OPEN_OR_PENDING"
    if len(active) >= max_concurrent_positions:
        return False, "MAX_CONCURRENT_POSITIONS_REACHED"
    return True, "CAPACITY_AVAILABLE"


def plan_v2_position_exit(
    position: Mapping[str, Any],
    *,
    mark_price: Any,
    bars_since_entry: int,
    lot_size: Any,
    min_size: Any,
    fee_rate_per_side: Any,
    tick_size: Any | None = None,
) -> dict[str, Any]:
    """Return an idempotency-friendly TP1/TIME_EXIT action plan, never an order.

    TP1 is 1.2R and closes exactly half the current entry size, rounded down to
    the contract lot. Net break-even includes estimated round-trip fees only;
    funding/slippage are intentionally not guessed and must be supplied by a
    richer adapter before a real stop amendment is allowed.
    """
    try:
        side = str(position["side"]).strip().upper()
        entry = Decimal(str(position["entry_price"]))
        stop = Decimal(str(position["initial_stop_loss"]))
        size = Decimal(str(position["remaining_size"]))
        price = Decimal(str(mark_price))
        lot = Decimal(str(lot_size))
        minimum = Decimal(str(min_size))
        fee = Decimal(str(fee_rate_per_side))
        tick = None if tick_size in (None, "") else Decimal(str(tick_size))
    except Exception as exc:
        raise V2PositionControlBlocked("Position exit inputs are incomplete.") from exc
    if side not in {"LONG", "SHORT"} or min(entry, stop, size, price, lot, minimum) <= 0:
        raise V2PositionControlBlocked("Position exit inputs are invalid.")
    if fee < 0 or fee > Decimal("0.01") or bars_since_entry < 0 or (tick is not None and tick <= 0):
        raise V2PositionControlBlocked("Fee rate or candle age is invalid.")
    risk = entry - stop if side == "LONG" else stop - entry
    if risk <= 0:
        raise V2PositionControlBlocked("Initial stop is on the wrong side of entry.")
    tp1 = entry + Decimal("1.2") * risk if side == "LONG" else entry - Decimal("1.2") * risk
    already_taken = position.get("tp1_taken") is True
    if not already_taken and ((side == "LONG" and price >= tp1) or (side == "SHORT" and price <= tp1)):
        half = (size / Decimal("2") / lot).to_integral_value(rounding=ROUND_DOWN) * lot
        if half < minimum:
            raise V2PositionControlBlocked("Half-size TP1 close would be below contract minSize.")
        # Approximate fee-aware BE for entry+exit taker fees. Funding and slippage
        # are not inferable from this snapshot, so the result is explicitly estimated.
        fee_adjustment = (entry * fee * Decimal("2")) / (Decimal("1") - fee)
        net_be = entry + fee_adjustment if side == "LONG" else entry - fee_adjustment
        return {
            "action": "TP1_PARTIAL_CLOSE_AND_MOVE_STOP",
            "trigger_price": str(tp1),
            "close_size": str(half.normalize()),
            "remaining_size": str((size - half).normalize()),
            "net_break_even_stop": str(net_be.normalize()),
            "break_even_basis": "ENTRY_PLUS_ESTIMATED_ROUND_TRIP_TAKER_FEES_EXCLUDES_FUNDING_SLIPPAGE",
        }
    if not already_taken and bars_since_entry >= 12:
        fee_adjustment = (entry * fee * Decimal("2")) / (Decimal("1") - fee)
        net_be = entry + fee_adjustment if side == "LONG" else entry - fee_adjustment
        if tick is not None:
            rounding = ROUND_FLOOR if side == "LONG" else ROUND_CEILING
            net_be = (net_be / tick).to_integral_value(rounding=rounding) * tick
        return {
            "action": "TIME_EXIT_LIMIT_AT_NET_BREAK_EVEN",
            "limit_price": str(net_be.normalize()),
            "bars_since_entry": int(bars_since_entry),
            "break_even_basis": "ENTRY_PLUS_ESTIMATED_ROUND_TRIP_TAKER_FEES_EXCLUDES_FUNDING_SLIPPAGE",
        }
    return {"action": "HOLD", "tp1_price": str(tp1.normalize()), "bars_since_entry": int(bars_since_entry)}
