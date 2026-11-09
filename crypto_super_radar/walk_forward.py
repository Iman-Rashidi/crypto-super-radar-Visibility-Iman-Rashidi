"""Chronological walk-forward validation utilities.

The caller supplies a strategy-return series produced without future information.
This module splits it into disjoint train/test windows and reports each test fold.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class WalkForwardConfig:
    train_bars: int = 180
    test_bars: int = 45
    step_bars: int = 45
    embargo_bars: int = 0
    min_folds: int = 3
    fee_bps_per_side: float = 8.0
    slippage_bps_per_side: float = 10.0


def _metrics(returns: pd.Series) -> dict:
    r = pd.to_numeric(returns, errors="coerce")
    if r.empty or r.isna().any() or not np.isfinite(r.to_numpy()).all():
        return {"status": "UNKNOWN", "observations": int(r.notna().sum()),
                "sharpe": None, "profit_factor": None, "max_drawdown_pct": None,
                "total_return_pct": None}
    equity = (1.0 + r).cumprod()
    peak = equity.cummax()
    drawdown = (equity / peak - 1.0).min()
    gains = float(r[r > 0].sum())
    losses = float(-r[r < 0].sum())
    pf = None if losses == 0 else gains / losses
    std = float(r.std(ddof=1)) if len(r) > 1 else 0.0
    sharpe = None if std == 0 else float(r.mean() / std * np.sqrt(365.0))
    return {
        "status": "KNOWN", "observations": len(r), "sharpe": sharpe,
        "profit_factor": pf, "max_drawdown_pct": abs(float(drawdown)) * 100.0,
        "total_return_pct": (float(equity.iloc[-1]) - 1.0) * 100.0,
    }


def walk_forward_validate(
    strategy_returns: pd.Series,
    config: WalkForwardConfig = WalkForwardConfig(),
    *,
    funding_returns: pd.Series | None = None,
    perpetual: bool = False,
) -> dict:
    """Validate net returns across chronological test folds; no missing-value fill."""
    if not isinstance(strategy_returns.index, pd.DatetimeIndex):
        return {"status": "UNKNOWN", "reason": "A DatetimeIndex is required."}
    data = strategy_returns.sort_index()
    if data.index.has_duplicates or not data.index.is_monotonic_increasing:
        return {"status": "FAIL", "reason": "Timestamps must be unique and chronological."}
    if len(data) < config.train_bars + config.embargo_bars + config.test_bars:
        return {"status": "UNKNOWN", "reason": "Insufficient observations for the first train/test fold."}
    if perpetual:
        if funding_returns is None:
            return {"status": "UNKNOWN", "reason": "Historical funding is mandatory for perpetual validation."}
        funding = funding_returns.reindex(data.index)
        if funding.isna().any():
            return {"status": "UNKNOWN", "reason": "Missing historical funding observations; funding is never assumed to be zero."}
        data = data - funding
    cost = 2.0 * (config.fee_bps_per_side + config.slippage_bps_per_side) / 10_000.0
    data = data - cost
    folds = []
    train_end = config.train_bars
    while train_end + config.embargo_bars + config.test_bars <= len(data):
        test_start = train_end + config.embargo_bars
        test_end = test_start + config.test_bars
        train = data.iloc[:train_end]
        test = data.iloc[test_start:test_end]
        if train.index.max() >= test.index.min():
            return {"status": "FAIL", "reason": "Temporal leakage detected between train and test."}
        folds.append({
            "train_start": train.index.min().isoformat(),
            "train_end": train.index.max().isoformat(),
            "test_start": test.index.min().isoformat(),
            "test_end": test.index.max().isoformat(),
            "train_metrics": _metrics(train),
            "test_metrics": _metrics(test),
        })
        train_end += config.step_bars
    if len(folds) < config.min_folds:
        return {"status": "UNKNOWN", "reason": "Too few complete out-of-sample folds.",
                "fold_count": len(folds), "folds": folds}
    test_returns = pd.concat([
        data.loc[pd.to_datetime(f["test_start"]):pd.to_datetime(f["test_end"])]
        for f in folds
    ])
    aggregate = _metrics(test_returns)
    worst_drawdown = max(
        (f["test_metrics"]["max_drawdown_pct"] or 0.0) for f in folds
    )
    return {
        "status": "KNOWN", "fold_count": len(folds), "folds": folds,
        "aggregate_oos": aggregate, "worst_fold_drawdown_pct": worst_drawdown,
        "cost_per_bar_pct": cost * 100.0,
        "note": "Train windows are chronological; only later test windows contribute to aggregate OOS metrics.",
    }
