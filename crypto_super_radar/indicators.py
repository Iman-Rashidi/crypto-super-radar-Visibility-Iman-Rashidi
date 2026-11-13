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
    if last > ema50.iloc[-1] > ema200.iloc[-1] and asset_ret30 > 0:
        asset_regime = "BULLISH"
    elif last < ema50.iloc[-1] < ema200.iloc[-1] and asset_ret30 < 0:
        asset_regime = "BEARISH"
    else:
        asset_regime = "NEUTRAL"
    return {
        "status": "KNOWN",
        "asset_regime": asset_regime,
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



def observed_price_ranges(
    candles: pd.DataFrame,
    *,
    windows_days: tuple[int, ...] = (7, 30, 90, 365),
    now: Optional[pd.Timestamp] = None,
    max_age_hours: float = 48.0,
) -> Dict[str, Any]:
    """Calculate observed OHLC extrema only when the requested window is adequately covered.

    These are historical range extrema, not guaranteed support/resistance levels.
    Missing, malformed, stale, duplicated or future-dated candles are never
    silently treated as valid coverage.
    """
    required = {"open", "high", "low", "close"}
    unknown = {"status": "UNKNOWN", "reason": "Validated OHLC candles are unavailable.", "windows": {}}
    if candles is None or candles.empty or not required.issubset(candles.columns):
        return unknown
    if not isinstance(candles.index, pd.DatetimeIndex):
        return {**unknown, "reason": "Candle timestamps are not a DatetimeIndex."}
    frame = candles.loc[:, ["open", "high", "low", "close"]].copy()
    if frame.index.tz is None:
        frame.index = frame.index.tz_localize("UTC")
    else:
        frame.index = frame.index.tz_convert("UTC")
    if frame.index.has_duplicates:
        return {**unknown, "reason": "Duplicate candle timestamps prevent reliable range coverage."}
    frame = frame.sort_index()
    for column in required:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    invalid = (
        frame[list(required)].isna().any(axis=1)
        | (frame["high"] < frame["low"])
        | (frame["high"] < frame["open"])
        | (frame["high"] < frame["close"])
        | (frame["low"] > frame["open"])
        | (frame["low"] > frame["close"])
        | (frame[list(required)] <= 0).any(axis=1)
    )
    if invalid.any():
        return {**unknown, "reason": "Malformed OHLC candles detected; range coverage is UNKNOWN."}
    current = now or pd.Timestamp.now(tz="UTC")
    if current.tzinfo is None:
        current = current.tz_localize("UTC")
    else:
        current = current.tz_convert("UTC")
    latest = frame.index[-1]
    age_hours = float((current - latest).total_seconds() / 3600.0)
    if age_hours < -1.0 or age_hours > max_age_hours:
        return {**unknown, "reason": "Latest candle is future-dated or stale.", "candle_age_hours": age_hours}
    deltas = frame.index.to_series().diff().dropna().dt.total_seconds()
    positive = deltas[deltas > 0]
    if positive.empty:
        return {**unknown, "reason": "Insufficient timestamp history to infer candle cadence."}
    cadence_seconds = float(positive.median())
    if cadence_seconds <= 0:
        return {**unknown, "reason": "Invalid candle cadence."}
    windows: Dict[str, Any] = {}
    for days in windows_days:
        if not isinstance(days, int) or days <= 0:
            continue
        cutoff = current - pd.Timedelta(days=days)
        sample = frame.loc[(frame.index >= cutoff) & (frame.index <= current)]
        expected = max(1.0, days * 86400.0 / cadence_seconds)
        coverage = min(1.0, len(sample) / expected)
        starts_near_window = bool(len(sample)) and sample.index[0] <= cutoff + pd.Timedelta(seconds=1.5 * cadence_seconds)
        if len(sample) < 2 or coverage < 0.80 or not starts_near_window:
            windows[str(days)] = {
                "status": "UNKNOWN", "high": None, "low": None,
                "coverage_pct": round(coverage * 100.0, 2),
                "candle_count": int(len(sample)),
                "reason": "Insufficient candle coverage for this lookback window.",
            }
            continue
        high_at = sample["high"].idxmax()
        low_at = sample["low"].idxmin()
        windows[str(days)] = {
            "status": "KNOWN",
            "high": float(sample.loc[high_at, "high"]),
            "low": float(sample.loc[low_at, "low"]),
            "high_at": high_at.isoformat(),
            "low_at": low_at.isoformat(),
            "coverage_pct": round(coverage * 100.0, 2),
            "candle_count": int(len(sample)),
            "start": sample.index[0].isoformat(),
            "end": sample.index[-1].isoformat(),
        }
    known = any(item["status"] == "KNOWN" for item in windows.values())
    return {
        "status": "KNOWN" if known else "UNKNOWN",
        "reason": None if known else "No lookback window has sufficient validated coverage.",
        "candle_age_hours": age_hours,
        "cadence_seconds": cadence_seconds,
        "current_price": float(frame["close"].iloc[-1]),
        "windows": windows,
    }


def simulate_spot_long(
    investment_eur: float,
    price_change_pct: float,
    *,
    buy_fee_pct: float = 0.10,
    sell_fee_pct: float = 0.10,
    buy_slippage_pct: float = 0.20,
    sell_slippage_pct: float = 0.20,
) -> Dict[str, Any]:
    """Scenario calculator for an unleveraged EUR-denominated spot long.

    Excludes taxes, EUR/USD FX changes, spreads beyond supplied slippage,
    custody costs and market impact. It is not a forecast or a return promise.
    """
    values = (investment_eur, price_change_pct, buy_fee_pct, sell_fee_pct,
              buy_slippage_pct, sell_slippage_pct)
    if any(not np.isfinite(value) for value in values):
        raise ValueError("All calculator inputs must be finite numbers.")
    if investment_eur <= 0:
        raise ValueError("Investment must be greater than zero.")
    if price_change_pct <= -100:
        raise ValueError("Price change cannot be -100% or lower.")
    for fee in (buy_fee_pct, sell_fee_pct, buy_slippage_pct, sell_slippage_pct):
        if fee < 0 or fee >= 100:
            raise ValueError("Fee and slippage assumptions must be between 0% and 100%.")
    buy_fee = investment_eur * buy_fee_pct / 100.0
    after_buy_fee = investment_eur - buy_fee
    buy_slippage_cost = after_buy_fee * buy_slippage_pct / 100.0
    invested_exposure = after_buy_fee - buy_slippage_cost
    marked_value = invested_exposure * (1.0 + price_change_pct / 100.0)
    sell_slippage_cost = marked_value * sell_slippage_pct / 100.0
    after_sell_slippage = marked_value - sell_slippage_cost
    sell_fee = after_sell_slippage * sell_fee_pct / 100.0
    net_proceeds = after_sell_slippage - sell_fee
    net_pnl = net_proceeds - investment_eur
    multiplier = ((1.0 - buy_fee_pct / 100.0)
                  * (1.0 - buy_slippage_pct / 100.0)
                  * (1.0 - sell_slippage_pct / 100.0)
                  * (1.0 - sell_fee_pct / 100.0))
    break_even_pct = (1.0 / multiplier - 1.0) * 100.0
    return {
        "investment_eur": float(investment_eur),
        "price_change_pct": float(price_change_pct),
        "buy_fee_eur": float(buy_fee),
        "buy_slippage_eur": float(buy_slippage_cost),
        "sell_slippage_eur": float(sell_slippage_cost),
        "sell_fee_eur": float(sell_fee),
        "net_proceeds_eur": float(net_proceeds),
        "net_pnl_eur": float(net_pnl),
        "net_return_pct": float(net_pnl / investment_eur * 100.0),
        "break_even_price_change_pct": float(break_even_pct),
        "assumptions": {
            "buy_fee_pct": float(buy_fee_pct),
            "sell_fee_pct": float(sell_fee_pct),
            "buy_slippage_pct": float(buy_slippage_pct),
            "sell_slippage_pct": float(sell_slippage_pct),
        },
    }
