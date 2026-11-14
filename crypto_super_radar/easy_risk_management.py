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



def calculate_fractional_kelly(
    win_probability: float,
    average_win_loss_ratio: float,
    *,
    sample_size: int,
    fraction: float = 0.25,
    max_bankroll_fraction: float = 0.05,
    minimum_sample_size: int = 30,
) -> dict[str, Any]:
    """Compute a capped fractional-Kelly diagnostic only with a minimum sample."""
    p, ratio = _positive(win_probability), _positive(average_win_loss_ratio)
    if p is None or p >= 1 or ratio is None:
        return {"status": "UNKNOWN", "reason": "Win probability must be (0,1) and win/loss ratio positive."}
    if sample_size < minimum_sample_size:
        return {"status": "UNKNOWN", "reason": "Out-of-sample trade count is below the minimum for Kelly sizing.",
                "sample_size": sample_size, "minimum_sample_size": minimum_sample_size}
    if not 0 < fraction <= 1 or not 0 < max_bankroll_fraction <= 0.25:
        return {"status": "UNKNOWN", "reason": "Kelly fraction or cap is outside its safe bounds."}
    full = p - (1.0 - p) / ratio
    fractional = max(0.0, full * fraction)
    applied = min(fractional, max_bankroll_fraction)
    return {
        "status": "KNOWN", "full_kelly_fraction": full,
        "fractional_kelly_fraction": fractional, "applied_bankroll_fraction": applied,
        "kelly_multiplier": fraction, "max_bankroll_fraction": max_bankroll_fraction,
        "sample_size": sample_size, "minimum_sample_size": minimum_sample_size,
        "used_for_default_position_sizing": False,
        "warning": "Kelly is highly sensitive to estimated win rate and payoff; use only as a capped diagnostic.",
    }


def historical_var_expected_shortfall(
    returns: Any,
    *,
    confidence: float = 0.95,
    minimum_observations: int = 60,
) -> dict[str, Any]:
    """Historical one-period VaR/Expected Shortfall from observed fractional returns."""
    import numpy as np
    try:
        values = np.asarray(list(returns), dtype=float)
    except (TypeError, ValueError):
        return {"status": "UNKNOWN", "reason": "Return observations are not numeric."}
    values = values[np.isfinite(values)]
    if not 0.5 < confidence < 1 or len(values) < minimum_observations:
        return {"status": "UNKNOWN", "reason": "Confidence or historical observation count is insufficient.",
                "observation_count": int(len(values)), "minimum_observations": minimum_observations}
    if np.any(values < -1.0):
        return {"status": "UNKNOWN", "reason": "Returns below -100% are invalid for a standard long-only return series."}
    cutoff = float(np.quantile(values, 1.0 - confidence, method="linear"))
    tail = values[values <= cutoff]
    es = float(np.mean(tail)) if len(tail) else cutoff
    return {
        "status": "KNOWN", "method": "HISTORICAL",
        "confidence": confidence, "observation_count": int(len(values)),
        "var_loss_pct": max(0.0, -cutoff * 100.0),
        "expected_shortfall_loss_pct": max(0.0, -es * 100.0),
        "tail_observation_count": int(len(tail)),
        "horizon": "one observation period",
        "note": "Historical estimates do not bound future losses or capture unobserved tail events.",
    }


def stress_test_positions(
    positions: list[dict[str, Any]],
    scenarios: Optional[dict[str, float]] = None,
) -> dict[str, Any]:
    """Apply transparent parallel price shocks to unlevered position notionals."""
    shocks = scenarios or {"MILD_DRAWDOWN": -0.10, "SEVERE_DRAWDOWN": -0.20, "EXTREME_DRAWDOWN": -0.35}
    valid = []
    for item in positions:
        if not isinstance(item, dict):
            return {"status": "UNKNOWN", "reason": "Position entries must be objects."}
        notional = _positive(item.get("notional"))
        side = str(item.get("side", "LONG")).upper()
        if notional is None or side not in {"LONG", "SHORT"}:
            return {"status": "UNKNOWN", "reason": "Each position needs a positive notional and LONG/SHORT side."}
        valid.append((notional, side))
    result = {}
    for name, shock in shocks.items():
        try:
            move = float(shock)
        except (TypeError, ValueError):
            return {"status": "UNKNOWN", "reason": "Scenario shock is not numeric."}
        if not math.isfinite(move) or not -1.0 <= move <= 1.0:
            return {"status": "UNKNOWN", "reason": "Scenario shock must be between -100% and +100%."}
        pnl = sum(n * move * (1 if side == "LONG" else -1) for n, side in valid)
        result[str(name)] = {"market_shock_pct": move * 100.0, "pnl_estimate": pnl}
    return {"status": "KNOWN", "scenarios": result,
            "note": "Parallel shocks ignore basis risk, liquidation, fees, funding, slippage and nonlinear derivatives payoff."}


def portfolio_risk_summary(
    positions: list[dict[str, Any]],
    equity: float,
) -> dict[str, Any]:
    """Aggregate stop-distance risk and gross notionals across current positions."""
    capital = _positive(equity)
    if capital is None:
        return {"status": "UNKNOWN", "reason": "Positive portfolio equity is required."}
    rows = []
    for item in positions:
        if not isinstance(item, dict):
            return {"status": "UNKNOWN", "reason": "Position entries must be objects."}
        qty = _positive(item.get("quantity"))
        entry = _positive(item.get("entry"))
        stop = _positive(item.get("stop"))
        side = str(item.get("side", "LONG")).upper()
        if qty is None or entry is None or stop is None or side not in {"LONG", "SHORT"}:
            return {"status": "UNKNOWN", "reason": "Each position needs positive quantity, entry, stop and side."}
        directionally_valid = stop < entry if side == "LONG" else stop > entry
        if not directionally_valid:
            return {"status": "UNKNOWN", "reason": "Stop is on the invalid side of entry."}
        rows.append((qty * entry, qty * abs(entry-stop)))
    notional = sum(row[0] for row in rows)
    stop_risk = sum(row[1] for row in rows)
    return {
        "status": "KNOWN", "position_count": len(rows),
        "gross_notional": notional, "stop_risk": stop_risk,
        "gross_exposure_pct_equity": notional / capital * 100.0,
        "open_stop_risk_pct_equity": stop_risk / capital * 100.0,
        "note": "Stop risk is an estimate; gaps and execution costs can increase realized losses.",
    }


def portfolio_correlation_analysis(
    returns_by_asset: dict[str, Any],
    notionals: dict[str, float],
    *,
    minimum_observations: int = 30,
) -> dict[str, Any]:
    """Calculate pairwise historical correlations and notional-weighted volatility."""
    import numpy as np
    import pandas as pd
    try:
        frame = pd.DataFrame({str(k): pd.Series(v, dtype=float) for k, v in returns_by_asset.items()})
        weights = {str(k): _positive(v) for k, v in notionals.items()}
    except Exception:
        return {"status": "UNKNOWN", "reason": "Asset returns or notionals are malformed."}
    common = frame.dropna()
    if frame.shape[1] < 2 or len(common) < minimum_observations:
        return {"status": "UNKNOWN", "reason": "At least two assets and 30 common return observations are required.",
                "common_observations": int(len(common))}
    if any(v is None for v in weights.values()) or set(weights) != set(frame.columns):
        return {"status": "UNKNOWN", "reason": "Every return series needs a positive matching notional."}
    corr = common.corr()
    weight_sum = sum(float(weights[k]) for k in frame.columns)
    w = np.asarray([float(weights[k]) / weight_sum for k in frame.columns])
    cov = common.cov().to_numpy(dtype=float)
    variance = float(w @ cov @ w)
    return {
        "status": "KNOWN", "common_observations": int(len(common)),
        "correlation_matrix": {str(k): {str(c): float(corr.loc[k, c]) for c in corr.columns} for k in corr.index},
        "weighted_daily_volatility_pct": math.sqrt(max(0.0, variance)) * 100.0,
        "highest_pairwise_correlation": float(corr.where(~(corr == 1.0)).max().max()),
        "note": "Historical correlations are unstable and do not guarantee diversification during market stress.",
    }


def estimate_beta_hedge(
    asset_returns: Any,
    hedge_returns: Any,
    portfolio_exposure: float,
    hedge_price: float,
    *,
    hedge_ratio: float = 1.0,
    minimum_observations: int = 30,
) -> dict[str, Any]:
    """Estimate a simple OLS beta and corresponding opposing hedge notional."""
    import numpy as np
    try:
        a, h = np.asarray(list(asset_returns), dtype=float), np.asarray(list(hedge_returns), dtype=float)
    except (TypeError, ValueError):
        return {"status": "UNKNOWN", "reason": "Return series are not numeric."}
    n = min(len(a), len(h))
    if n < minimum_observations:
        return {"status": "UNKNOWN", "reason": "At least 30 aligned return observations are required."}
    a, h = a[-n:], h[-n:]
    valid = np.isfinite(a) & np.isfinite(h)
    a, h = a[valid], h[valid]
    exposure, price = _positive(portfolio_exposure), _positive(hedge_price)
    if len(a) < minimum_observations or exposure is None or price is None or not 0 < hedge_ratio <= 2:
        return {"status": "UNKNOWN", "reason": "Exposure, price, hedge ratio or aligned history is invalid."}
    variance = float(np.var(h, ddof=1))
    if variance <= 1e-12:
        return {"status": "UNKNOWN", "reason": "Hedge benchmark variance is too small for beta estimation."}
    beta = float(np.cov(a, h, ddof=1)[0, 1] / variance)
    notional = abs(exposure * beta * hedge_ratio)
    return {
        "status": "KNOWN", "beta": beta, "hedge_ratio": hedge_ratio,
        "hedge_side": "SHORT" if beta >= 0 else "LONG",
        "hedge_notional": notional, "hedge_quantity": notional / price,
        "observation_count": int(len(a)), "used_for_automatic_execution": False,
        "note": "OLS hedge estimate; basis risk and rolling beta instability remain. No hedge order is submitted.",
    }
