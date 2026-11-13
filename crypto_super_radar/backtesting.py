"""Leakage-aware walk-forward evaluation for a fixed EMA trend rule."""

from __future__ import annotations

from typing import Any, Dict

import numpy as np
import pandas as pd

from .config import AppConfig


def _max_drawdown(returns: pd.Series) -> float:
    equity = (1.0 + returns.fillna(0.0)).cumprod()
    peak = equity.cummax()
    drawdown = equity / peak - 1.0
    return float(abs(drawdown.min()) * 100.0)


def walk_forward_backtest(candles: pd.DataFrame, config: AppConfig, allow_short: bool = False, funding_rates: pd.Series | None = None) -> Dict[str, Any]:
    """Chronological 70/30 split; signals are shifted to avoid look-ahead."""
    if candles is None or "close" not in candles or len(candles) < config.thresholds.min_history_bars:
        return {"status": "UNKNOWN", "reason": "Insufficient historical candles for walk-forward evaluation."}
    close = pd.to_numeric(candles["close"], errors="coerce").dropna()
    if len(close) < config.thresholds.min_history_bars:
        return {"status": "UNKNOWN", "reason": "Insufficient valid closes after cleaning."}
    fast = close.ewm(span=20, adjust=False).mean()
    slow = close.ewm(span=50, adjust=False).mean()
    if allow_short:
        raw_position = pd.Series(np.where(fast > slow, 1.0, np.where(fast < slow, -1.0, 0.0)), index=close.index)
    else:
        raw_position = (fast > slow).astype(float)
    # A signal formed on bar t can only earn the return from t to t+1.
    position = raw_position.shift(1).fillna(0.0)
    asset_returns = close.pct_change().fillna(0.0)
    turnover = position.diff().abs().fillna(position.abs())
    one_way_cost = (config.risk.fee_bps_per_side + config.risk.slippage_bps_per_side) / 10_000.0
    net_returns = position * asset_returns - turnover * one_way_cost
    split = int(len(net_returns) * 0.70)
    if allow_short:
        if funding_rates is None or funding_rates.empty:
            return {"status": "UNKNOWN", "reason": "Historical funding is required for a perpetual out-of-sample test."}
        funding_rates = pd.to_numeric(funding_rates, errors="coerce").dropna().sort_index()
        if not isinstance(funding_rates.index, pd.DatetimeIndex):
            return {"status": "UNKNOWN", "reason": "Funding history timestamps are unreadable."}
        funding_daily = funding_rates.groupby(funding_rates.index.floor("D")).sum()
        aligned_funding = funding_daily.reindex(net_returns.index.normalize(), fill_value=np.nan)
        oos_funding = aligned_funding.iloc[split:]
        coverage = float(oos_funding.notna().mean()) if len(oos_funding) else 0.0
        if coverage < 0.90:
            return {"status": "UNKNOWN", "reason": f"Historical funding covers only {coverage:.1%} of the out-of-sample window."}
        net_returns = net_returns - position * aligned_funding.fillna(0.0)
    split = int(len(net_returns) * 0.70)
    oos = net_returns.iloc[split:].copy()
    oos_position = position.iloc[split:]
    if len(oos) < config.thresholds.min_oos_bars:
        return {"status": "UNKNOWN", "reason": "Out-of-sample segment is too short."}
    daily_std = float(oos.std(ddof=1))
    sharpe = float(np.sqrt(365.0) * oos.mean() / daily_std) if daily_std > 0 else 0.0
    downside = oos[oos < 0]
    downside_std = float(np.sqrt((downside.pow(2)).mean())) if len(downside) else 0.0
    sortino = float(np.sqrt(365.0) * oos.mean() / downside_std) if downside_std > 0 else None
    positive_sum = float(oos[oos > 0].sum())
    negative_sum = float(abs(oos[oos < 0].sum()))
    profit_factor = positive_sum / negative_sum if negative_sum > 0 else (float("inf") if positive_sum > 0 else 0.0)
    # Count completed exposure changes in the OOS interval; not equivalent to fills.
    previous_position = oos_position.shift(1).fillna(0.0)
    entries = (oos_position.ne(0.0) & ((previous_position.eq(0.0)) | (oos_position * previous_position < 0.0)))
    trades = int(entries.sum())
    total_return = float((1.0 + oos).prod() - 1.0) * 100.0
    max_dd = _max_drawdown(oos)
    thresholds = config.thresholds
    passed = (
        sharpe >= thresholds.min_oos_sharpe
        and profit_factor >= thresholds.min_oos_profit_factor
        and max_dd < thresholds.max_backtest_drawdown_pct
        and trades >= 3
    )
    return {
        "status": "PASS" if passed else "FAIL",
        "oos_sharpe": sharpe,
        "oos_sortino": sortino,
        "oos_profit_factor": profit_factor,
        "oos_max_drawdown_pct": max_dd,
        "oos_net_return_pct": total_return,
        "oos_trade_transitions": trades,
        "oos_bars": len(oos),
        "cost_assumption_bps_per_side": config.risk.fee_bps_per_side + config.risk.slippage_bps_per_side,
        "strategy": "EMA20/EMA50 trend; fixed parameters; chronological 70/30 split",
        "funding_costs_included": bool(allow_short),
        "reason": "Passes initial out-of-sample gates." if passed else "Fails one or more out-of-sample thresholds.",
    }
