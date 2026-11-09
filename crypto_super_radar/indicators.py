"""Pandas-based swing indicators and market-regime calculations."""

from __future__ import annotations

from typing import Any, Dict, Optional

import numpy as np
import pandas as pd


def _clean_close(candles: pd.DataFrame) -> pd.Series:
    if candles is None or "close" not in candles:
        return pd.Series(dtype=float)
    return pd.to_numeric(candles["close"], errors="coerce").dropna().astype(float)


def rsi(close: pd.Series, period: int = 14) -> Optional[float]:
    """Wilder RSI; returns None instead of fabricating a value."""
    close = pd.to_numeric(close, errors="coerce").dropna()
    if len(close) < period + 1:
        return None
    delta = close.diff()
    gains = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    losses = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_gain, avg_loss = gains.iloc[-1], losses.iloc[-1]
    if pd.isna(avg_gain) or pd.isna(avg_loss):
        return None
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return float(100.0 - 100.0 / (1.0 + rs))


def atr(candles: pd.DataFrame, period: int = 14) -> Optional[float]:
    required = {"high", "low", "close"}
    if candles is None or not required.issubset(candles.columns) or len(candles) < period + 1:
        return None
    high = pd.to_numeric(candles["high"], errors="coerce")
    low = pd.to_numeric(candles["low"], errors="coerce")
    close = pd.to_numeric(candles["close"], errors="coerce")
    previous_close = close.shift(1)
    true_range = pd.concat([
        high - low,
        (high - previous_close).abs(),
        (low - previous_close).abs(),
    ], axis=1).max(axis=1)
    value = true_range.ewm(alpha=1 / period, adjust=False, min_periods=period).mean().iloc[-1]
    return float(value) if pd.notna(value) and value > 0 else None


def market_regime(benchmark: pd.DataFrame) -> Dict[str, Any]:
    close = _clean_close(benchmark)
    if len(close) < 200:
        return {"regime": "UNKNOWN", "return_30d_pct": None, "ema50": None, "ema200": None}
    ema50 = close.ewm(span=50, adjust=False).mean().iloc[-1]
    ema200 = close.ewm(span=200, adjust=False).mean().iloc[-1]
    ret30 = (close.iloc[-1] / close.iloc[-31] - 1.0) * 100.0
    if ema50 > ema200 and ret30 > 0:
        regime = "BULLISH"
    elif ema50 < ema200 and ret30 < 0:
        regime = "BEARISH"
    else:
        regime = "NEUTRAL"
    return {"regime": regime, "return_30d_pct": float(ret30), "ema50": float(ema50), "ema200": float(ema200)}


def technical_metrics(candles: pd.DataFrame, benchmark: pd.DataFrame) -> Dict[str, Any]:
    close = _clean_close(candles)
    benchmark_close = _clean_close(benchmark)
    if len(close) < 200 or len(benchmark_close) < 31:
        return {"status": "UNKNOWN", "reason": "At least 200 asset candles and 31 benchmark candles are required."}
    ema20 = close.ewm(span=20, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    ema200 = close.ewm(span=200, adjust=False).mean()
    last = float(close.iloc[-1])
    rsi_value = rsi(close)
    rsi_previous = rsi(close.iloc[:-1]) if len(close) >= 16 else None
    rsi_change = (
        float(rsi_value - rsi_previous)
        if rsi_value is not None and rsi_previous is not None else None
    )
    atr_value = atr(candles)
    if rsi_value is None or atr_value is None:
        return {"status": "UNKNOWN", "reason": "RSI or ATR could not be calculated."}
    asset_ret30 = (close.iloc[-1] / close.iloc[-31] - 1.0) * 100.0
    benchmark_ret30 = (benchmark_close.iloc[-1] / benchmark_close.iloc[-31] - 1.0) * 100.0
    relative_strength = float(asset_ret30 - benchmark_ret30)
    long_structure = last > ema20.iloc[-1] > ema50.iloc[-1] > ema200.iloc[-1]
    short_structure = last < ema20.iloc[-1] < ema50.iloc[-1] < ema200.iloc[-1]
    direction = "LONG" if long_structure else "SHORT" if short_structure else "NONE"
    return {
        "status": "KNOWN",
        "close": last,
        "ema20": float(ema20.iloc[-1]),
        "ema50": float(ema50.iloc[-1]),
        "ema200": float(ema200.iloc[-1]),
        "rsi14": float(rsi_value),
        "rsi14_previous": float(rsi_previous) if rsi_previous is not None else None,
        "rsi14_change": rsi_change,
        "atr14": float(atr_value),
        "asset_return_30d_pct": float(asset_ret30),
        "benchmark_return_30d_pct": float(benchmark_ret30),
        "relative_strength_30d_pct": relative_strength,
        "direction": direction,
    }


def latest_candle_age_hours(candles: pd.DataFrame, now: Optional[pd.Timestamp] = None) -> Optional[float]:
    if candles is None or candles.empty or not isinstance(candles.index, pd.DatetimeIndex):
        return None
    timestamp = candles.index.max()
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    now = now or pd.Timestamp.now(tz="UTC")
    return max(0.0, float((now - timestamp).total_seconds() / 3600.0))
