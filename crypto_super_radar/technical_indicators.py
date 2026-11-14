"""Technical-indicator evidence for the Easy Trader engine.

Signals are descriptive evidence only. They never authorize an order by themselves.
"""
from __future__ import annotations

from typing import Any
import math

import numpy as np
import pandas as pd


def _unknown(reason: str) -> dict[str, Any]:
    return {"status": "UNKNOWN", "reason": reason}


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def _last_two_pivots(values: np.ndarray, kind: str, start: int = 2) -> list[tuple[int, float]]:
    pivots: list[tuple[int, float]] = []
    for i in range(start, len(values) - 2):
        window = values[i - 2:i + 3]
        if kind == "HIGH" and values[i] == np.max(window) and values[i] > np.max(window[:2]):
            pivots.append((i, float(values[i])))
        elif kind == "LOW" and values[i] == np.min(window) and values[i] < np.min(window[:2]):
            pivots.append((i, float(values[i])))
    return pivots[-2:]


def analyze_technical_indicators(candles: pd.DataFrame, *, envelope_pct: float = 5.0) -> dict[str, Any]:
    """Calculate SMA/EMA, OBV and RSI strategies using completed OHLCV rows."""
    required = {"open", "high", "low", "close", "volume"}
    if not isinstance(candles, pd.DataFrame) or not required.issubset(candles.columns):
        return _unknown("OHLCV candles are unavailable.")
    if len(candles) < 30:
        return _unknown("At least 30 candles are required for technical indicators.")
    frame = candles.loc[:, sorted(required)].copy()
    for column in required:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    values = frame[list(required)].to_numpy(dtype=float)
    if not np.isfinite(values).all() or (values <= 0).any():
        return _unknown("OHLCV data contains missing, non-finite, or non-positive values.")
    if ((frame.high < frame.low) | (frame.high < frame.open) | (frame.high < frame.close)
            | (frame.low > frame.open) | (frame.low > frame.close)).any():
        return _unknown("Malformed OHLC relationships were detected.")
    try:
        envelope_pct = float(envelope_pct)
    except (TypeError, ValueError, OverflowError):
        return _unknown("Envelope percentage is invalid.")
    if not math.isfinite(envelope_pct) or not 0 < envelope_pct <= 20:
        return _unknown("Envelope percentage must be between 0 and 20.")

    close, high, low, volume = (frame[name].astype(float) for name in ("close", "high", "low", "volume"))
    last = float(close.iloc[-1])
    sma = {p: close.rolling(p, min_periods=p).mean() for p in (20, 50, 200)}
    ema = {p: close.ewm(span=p, adjust=False, min_periods=p).mean() for p in (20, 50, 200)}
    ma_values = {
        "sma20": float(sma[20].iloc[-1]) if _finite(sma[20].iloc[-1]) else None,
        "sma50": float(sma[50].iloc[-1]) if _finite(sma[50].iloc[-1]) else None,
        "sma200": float(sma[200].iloc[-1]) if _finite(sma[200].iloc[-1]) else None,
        "ema20": float(ema[20].iloc[-1]) if _finite(ema[20].iloc[-1]) else None,
        "ema50": float(ema[50].iloc[-1]) if _finite(ema[50].iloc[-1]) else None,
        "ema200": float(ema[200].iloc[-1]) if _finite(ema[200].iloc[-1]) else None,
    }

    def cross(short: pd.Series, long: pd.Series) -> str:
        if len(short) < 2 or not all(_finite(x) for x in (short.iloc[-2], short.iloc[-1], long.iloc[-2], long.iloc[-1])):
            return "UNKNOWN"
        if short.iloc[-2] <= long.iloc[-2] and short.iloc[-1] > long.iloc[-1]:
            return "GOLDEN_CROSS"
        if short.iloc[-2] >= long.iloc[-2] and short.iloc[-1] < long.iloc[-1]:
            return "DEATH_CROSS"
        return "NONE"

    sma_cross = cross(sma[50], sma[200])
    ema_cross = cross(ema[50], ema[200])
    trend = "UNKNOWN"
    if ma_values["ema50"] is not None and ma_values["ema200"] is not None:
        trend = "BULLISH" if last > ma_values["ema50"] > ma_values["ema200"] else (
            "BEARISH" if last < ma_values["ema50"] < ma_values["ema200"] else "NEUTRAL"
        )
    true_range = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    atr = float(true_range.ewm(alpha=1/14, adjust=False, min_periods=14).mean().iloc[-1])
    pullback = "NONE"
    if _finite(atr) and atr > 0:
        for period in (20, 50):
            avg = ema[period].iloc[-1]
            if _finite(avg) and abs(last - float(avg)) <= 0.5 * atr:
                if trend == "BULLISH" and close.iloc[-1] >= close.iloc[-2]:
                    pullback = f"BULLISH_SUPPORT_EMA{period}"
                elif trend == "BEARISH" and close.iloc[-1] <= close.iloc[-2]:
                    pullback = f"BEARISH_RESISTANCE_EMA{period}"
                if pullback != "NONE":
                    break
    envelope_mid = ma_values["sma50"]
    envelopes = {
        "period": 50, "basis": "SMA",
        "upper": envelope_mid * (1 + envelope_pct / 100) if envelope_mid is not None else None,
        "lower": envelope_mid * (1 - envelope_pct / 100) if envelope_mid is not None else None,
        "percentage": envelope_pct,
    }
    if envelopes["upper"] is not None and last > envelopes["upper"]:
        envelopes["position"] = "ABOVE_UPPER"
    elif envelopes["lower"] is not None and last < envelopes["lower"]:
        envelopes["position"] = "BELOW_LOWER"
    else:
        envelopes["position"] = "INSIDE" if envelope_mid is not None else "UNKNOWN"

    obv = (np.sign(close.diff()).fillna(0.0) * volume).cumsum()
    obv_slope = float(obv.iloc[-1] - obv.iloc[-min(11, len(obv))])
    price_slope = float(close.iloc[-1] - close.iloc[-min(11, len(close))])
    obv_trend = "BULLISH" if obv_slope > 0 else "BEARISH" if obv_slope < 0 else "FLAT"
    price_trend = "BULLISH" if price_slope > 0 else "BEARISH" if price_slope < 0 else "FLAT"
    obv_divergence = "NONE"
    if len(close) >= 40:
        old_high, new_high = float(high.iloc[-40:-20].max()), float(high.iloc[-20:].max())
        old_low, new_low = float(low.iloc[-40:-20].min()), float(low.iloc[-20:].min())
        old_obv_high, new_obv_high = float(obv.iloc[-40:-20].max()), float(obv.iloc[-20:].max())
        old_obv_low, new_obv_low = float(obv.iloc[-40:-20].min()), float(obv.iloc[-20:].min())
        if new_high > old_high and new_obv_high <= old_obv_high:
            obv_divergence = "BEARISH"
        elif new_low < old_low and new_obv_low >= old_obv_low:
            obv_divergence = "BULLISH"
    obv_breakout = "UNCONFIRMED"
    if len(close) >= 21:
        resistance = float(high.iloc[-21:-1].max())
        support = float(low.iloc[-21:-1].min())
        if last > resistance and obv.iloc[-1] > obv.iloc[-2]:
            obv_breakout = "BULLISH_CONFIRMED"
        elif last < support and obv.iloc[-1] < obv.iloc[-2]:
            obv_breakout = "BEARISH_CONFIRMED"

    delta = close.diff()
    gains = delta.clip(lower=0).ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    losses = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    rs = gains / losses.replace(0, np.nan)
    rsi_series = 100 - 100 / (1 + rs)
    rsi_series = rsi_series.where(losses > 0, 100.0).where(gains > 0, 50.0)
    rsi_value = float(rsi_series.iloc[-1]) if _finite(rsi_series.iloc[-1]) else None
    rsi_previous = float(rsi_series.iloc[-2]) if len(rsi_series) > 1 and _finite(rsi_series.iloc[-2]) else None
    rsi_state = "UNKNOWN" if rsi_value is None else "OVERBOUGHT" if rsi_value > 70 else "OVERSOLD" if rsi_value < 30 else "NEUTRAL"
    midline = "UNKNOWN"
    if rsi_value is not None and rsi_previous is not None:
        midline = "BULLISH_CROSS" if rsi_previous <= 50 < rsi_value else (
            "BEARISH_CROSS" if rsi_previous >= 50 > rsi_value else "NONE"
        )
    rsi_divergence = "NONE"
    rsi_array = rsi_series.to_numpy(dtype=float)
    price_highs, price_lows = _last_two_pivots(high.to_numpy(dtype=float), "HIGH"), _last_two_pivots(low.to_numpy(dtype=float), "LOW")
    if len(price_highs) == 2 and all(_finite(rsi_array[i]) for i, _ in price_highs):
        (i1, p1), (i2, p2) = price_highs
        if p2 > p1 and rsi_array[i2] < rsi_array[i1] - 2:
            rsi_divergence = "BEARISH"
    if rsi_divergence == "NONE" and len(price_lows) == 2 and all(_finite(rsi_array[i]) for i, _ in price_lows):
        (i1, p1), (i2, p2) = price_lows
        if p2 < p1 and rsi_array[i2] > rsi_array[i1] + 2:
            rsi_divergence = "BULLISH"
    rsi_trendline = "UNKNOWN"
    rsi_valid = np.flatnonzero(np.isfinite(rsi_array))
    if len(rsi_valid) >= 12:
        segment = rsi_array[rsi_valid[-12:]]
        x = np.arange(len(segment), dtype=float)
        slope, intercept = np.polyfit(x[:-1], segment[:-1], 1)
        projected = slope * (len(segment) - 1) + intercept
        rsi_trendline = "BULLISH_BREAKOUT" if segment[-1] > projected and slope < 0 else (
            "BEARISH_BREAKDOWN" if segment[-1] < projected and slope > 0 else "NONE"
        )

    return {
        "status": "KNOWN",
        "moving_averages": {
            **ma_values, "trend": trend, "sma50_sma200_cross": sma_cross,
            "ema50_ema200_cross": ema_cross, "pullback": pullback,
            "envelopes": envelopes,
        },
        "obv": {
            "value": float(obv.iloc[-1]), "trend": obv_trend, "price_trend": price_trend,
            "trend_confirmation": obv_trend == price_trend,
            "divergence": obv_divergence, "breakout_confirmation": obv_breakout,
        },
        "rsi": {
            "period": 14, "value": rsi_value, "previous": rsi_previous,
            "state": rsi_state, "midline_50": midline,
            "divergence": rsi_divergence, "trendline_breakout": rsi_trendline,
        },
        "summary_fa": "میانگین‌های متحرک جهت روند، OBV همراهی حجم و RSI مومنتوم را می‌سنجند؛ این شواهد به‌تنهایی سیگنال ورود یا مجوز سفارش نیستند.",
    }
