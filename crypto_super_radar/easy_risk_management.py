"""Easy-bot risk sizing and market-cycle diagnostics.

All outputs are educational diagnostics. This module never places orders and
never changes mandatory decision gates.
"""
from __future__ import annotations

from typing import Any, Optional
import math


def _positive(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def calculate_position_size(
    equity: float,
    entry: float,
    stop: float,
    *,
    risk_pct: float = 0.5,
    fixed_risk_amount: Optional[float] = None,
    atr_value: Optional[float] = None,
    atr_multiple: float = 2.0,
    max_notional_pct: float = 100.0,
    fee_bps_per_side: float = 0.0,
    slippage_bps_per_side: float = 0.0,
) -> dict[str, Any]:
    """Return risk-budget sizing; quantity is based on stop distance, not leverage."""
    e, p, s = _positive(equity), _positive(entry), _positive(stop)
    if e is None or p is None or s is None or p == s:
        return {"status": "UNKNOWN", "reason": "Positive equity, entry and distinct stop are required."}
    if risk_pct <= 0 or risk_pct > 100 or max_notional_pct <= 0:
        return {"status": "UNKNOWN", "reason": "Risk and notional limits must be positive and valid."}
    distance = abs(p - s)
    if atr_value is not None:
        a = _positive(atr_value)
        if a is None or atr_multiple <= 0:
            return {"status": "UNKNOWN", "reason": "ATR inputs are invalid."}
        distance = max(distance, a * atr_multiple)
    budget = float(fixed_risk_amount) if fixed_risk_amount is not None else e * risk_pct / 100.0
    if not math.isfinite(budget) or budget <= 0:
        return {"status": "UNKNOWN", "reason": "Risk budget must be positive."}
    cost_rate = 2.0 * (max(0.0, fee_bps_per_side) + max(0.0, slippage_bps_per_side)) / 10000.0
    effective_loss_per_unit = distance + p * cost_rate
    if effective_loss_per_unit <= 0:
        return {"status": "UNKNOWN", "reason": "Effective loss per unit is invalid."}
    raw_qty = budget / effective_loss_per_unit
    max_notional = e * max_notional_pct / 100.0
    qty = min(raw_qty, max_notional / p)
    notional = qty * p
    modeled_loss = qty * effective_loss_per_unit
    return {
        "status": "KNOWN",
        "method": "FIXED_RISK_AMOUNT" if fixed_risk_amount is not None else "FIXED_EQUITY_PERCENT",
        "equity": e, "entry": p, "stop": s, "stop_distance": distance,
        "risk_budget": budget, "quantity": qty, "position_notional": notional,
        "modeled_loss_including_estimated_costs": modeled_loss,
        "estimated_cost_rate_pct": cost_rate * 100.0,
        "notional_cap_applied": qty < raw_qty,
        "risk_pct_of_equity": modeled_loss / e * 100.0,
        "note": "Sizing estimate only; gaps, slippage and stop execution can exceed modeled loss.",
    }


def assess_leverage_margin(
    position_notional: float,
    equity: float,
    leverage: float,
    *,
    maintenance_margin_rate: Optional[float] = None,
) -> dict[str, Any]:
    """Estimate initial margin; liquidation price is intentionally not fabricated."""
    n, e, lev = _positive(position_notional), _positive(equity), _positive(leverage)
    if n is None or e is None or lev is None:
        return {"status": "UNKNOWN", "reason": "Positive notional, equity and leverage are required."}
    initial_margin = n / lev
    result = {
        "status": "KNOWN", "position_notional": n, "equity": e, "leverage": lev,
        "initial_margin_estimate": initial_margin,
        "initial_margin_pct_of_equity": initial_margin / e * 100.0,
        "equity_remaining_after_initial_margin": e - initial_margin,
        "margin_exceeds_equity": initial_margin > e,
        "liquidation_price": None,
        "liquidation_status": "UNKNOWN",
        "note": "Liquidation depends on venue, maintenance tiers, fees, funding and position mode.",
    }
    mmr = _positive(maintenance_margin_rate) if maintenance_margin_rate is not None else None
    if maintenance_margin_rate is not None:
        if mmr is None or mmr >= 1:
            result["maintenance_margin_status"] = "UNKNOWN"
        else:
            result["maintenance_margin_estimate"] = n * mmr
            result["maintenance_margin_status"] = "ESTIMATE_ONLY"
    return result


def classify_market_cycle(candles: Any) -> dict[str, Any]:
    """Conservative four-phase heuristic from confirmed OHLCV history."""
    required = {"high", "low", "close", "volume"}
    if candles is None or not required.issubset(set(getattr(candles, "columns", []))):
        return {"status": "UNKNOWN", "phase": "UNKNOWN", "reason": "OHLCV history is incomplete."}
    frame = candles[list(required)].tail(60)
    if len(frame) < 30:
        return {"status": "UNKNOWN", "phase": "UNKNOWN", "reason": "At least 30 bars are required."}
    try:
        close = [float(x) for x in frame["close"]]
        volume = [float(x) for x in frame["volume"]]
        if any(not math.isfinite(x) or x <= 0 for x in close + volume):
            raise ValueError
    except (TypeError, ValueError):
        return {"status": "UNKNOWN", "phase": "UNKNOWN", "reason": "OHLCV values are invalid."}
    first, last = close[0], close[-1]
    move_pct = (last / first - 1.0) * 100.0
    midpoint = len(close) // 2
    first_half = close[:midpoint]
    second_half = close[midpoint:]
    range_pct = (max(close) - min(close)) / max(last, 1e-12) * 100.0
    early_volume = sum(volume[:midpoint]) / max(len(volume[:midpoint]), 1)
    late_volume = sum(volume[midpoint:]) / max(len(volume[midpoint:]), 1)
    volume_ratio = late_volume / early_volume if early_volume > 0 else None
    if abs(move_pct) < 4.0 and range_pct < 12.0:
        phase = "ACCUMULATION_CANDIDATE" if volume_ratio is not None and volume_ratio > 1.1 else "DISTRIBUTION_OR_RANGE"
    elif move_pct >= 4.0:
        phase = "MARK_UP"
    elif move_pct <= -4.0:
        phase = "MARK_DOWN"
    else:
        phase = "TRANSITION"
    return {
        "status": "KNOWN", "phase": phase, "move_pct": move_pct,
        "range_pct": range_pct, "volume_ratio_late_vs_early": volume_ratio,
        "heuristic": True, "used_as_hard_gate": False,
        "note": "Accumulation/distribution cannot be proven from price and volume alone.",
    }


STRATEGY_STYLE_GUIDE = {
    "SCALPING": {"holding_horizon": "seconds_to_minutes", "note": "Spread, fees and execution dominate."},
    "DAY_TRADING": {"holding_horizon": "intraday", "note": "Positions are intended to close within the session."},
    "SWING": {"holding_horizon": "days_to_weeks", "note": "Targets multi-session price swings."},
    "POSITION": {"holding_horizon": "weeks_to_months", "note": "Requires broader trend and thesis monitoring."},
}
