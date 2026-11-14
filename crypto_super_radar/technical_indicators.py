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
    true_range_values = true_range.to_numpy(dtype=float)
    atr_series = np.full(len(true_range_values), np.nan, dtype=float)
    if len(true_range_values) >= 14:
        atr_series[13] = float(np.mean(true_range_values[:14]))
        for i in range(14, len(true_range_values)):
            atr_series[i] = (atr_series[i - 1] * 13.0 + true_range_values[i]) / 14.0
    atr = float(atr_series[-1]) if _finite(atr_series[-1]) else float("nan")
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

    # MACD (12, 26, 9): line, signal, histogram and crossovers.
    macd_fast = close.ewm(span=12, adjust=False, min_periods=12).mean()
    macd_slow = close.ewm(span=26, adjust=False, min_periods=26).mean()
    macd_line = macd_fast - macd_slow
    macd_signal = macd_line.ewm(span=9, adjust=False, min_periods=9).mean()
    macd_hist = macd_line - macd_signal
    macd_cross = "UNKNOWN"
    if all(_finite(x) for x in (macd_line.iloc[-2], macd_line.iloc[-1], macd_signal.iloc[-2], macd_signal.iloc[-1])):
        if macd_line.iloc[-2] <= macd_signal.iloc[-2] and macd_line.iloc[-1] > macd_signal.iloc[-1]:
            macd_cross = "BULLISH_CROSS"
        elif macd_line.iloc[-2] >= macd_signal.iloc[-2] and macd_line.iloc[-1] < macd_signal.iloc[-1]:
            macd_cross = "BEARISH_CROSS"
        else:
            macd_cross = "NONE"
    macd_zero = "UNKNOWN"
    if _finite(macd_line.iloc[-2]) and _finite(macd_line.iloc[-1]):
        macd_zero = "BULLISH_ZERO_CROSS" if macd_line.iloc[-2] <= 0 < macd_line.iloc[-1] else (
            "BEARISH_ZERO_CROSS" if macd_line.iloc[-2] >= 0 > macd_line.iloc[-1] else "ABOVE_ZERO" if macd_line.iloc[-1] > 0 else "BELOW_ZERO"
        )
    macd_divergence = "NONE"
    macd_array = macd_line.to_numpy(dtype=float)
    if len(price_highs) == 2 and all(_finite(macd_array[i]) for i, _ in price_highs):
        (i1, p1), (i2, p2) = price_highs
        if p2 > p1 and macd_array[i2] < macd_array[i1]:
            macd_divergence = "BEARISH"
    if macd_divergence == "NONE" and len(price_lows) == 2 and all(_finite(macd_array[i]) for i, _ in price_lows):
        (i1, p1), (i2, p2) = price_lows
        if p2 < p1 and macd_array[i2] > macd_array[i1]:
            macd_divergence = "BULLISH"

    # Wilder ATR(14), independent of price direction.
    atr_value = float(atr) if _finite(atr) else None
    atr_pct_value = atr_value / last * 100.0 if atr_value is not None and last > 0 else None
    atr_risk = {
        "stop_distance_1x": atr_value,
        "stop_distance_1_5x": atr_value * 1.5 if atr_value is not None else None,
        "stop_distance_2x": atr_value * 2.0 if atr_value is not None else None,
        "long_stop_reference_2x": last - 2.0 * atr_value if atr_value is not None else None,
        "short_stop_reference_2x": last + 2.0 * atr_value if atr_value is not None else None,
        "position_sizing": "REQUIRES_ACCOUNT_EQUITY_AND_RISK_BUDGET",
    }

    # Parabolic SAR using Wilder's acceleration factor and reversal clamps.
    sar_values = np.full(len(frame), np.nan, dtype=float)
    sar_trend = "UNKNOWN"
    sar_flip = "NONE"
    sar_af = 0.02
    sar_max_af = 0.20
    if len(frame) >= 3:
        rising = close.iloc[1] >= close.iloc[0]
        sar = float(low.iloc[0] if rising else high.iloc[0])
        ep = float(high.iloc[1] if rising else low.iloc[1])
        sar_values[1] = sar
        for i in range(2, len(frame)):
            previous_sar = sar
            sar = previous_sar + sar_af * (ep - previous_sar)
            if rising:
                sar = min(sar, float(low.iloc[i - 1]), float(low.iloc[i - 2]))
                if low.iloc[i] < sar:
                    rising = False
                    sar = ep
                    ep = float(low.iloc[i])
                    sar_af = 0.02
                    sar_flip = "BEARISH"
                else:
                    if high.iloc[i] > ep:
                        ep = float(high.iloc[i])
                        sar_af = min(sar_af + 0.02, sar_max_af)
            else:
                sar = max(sar, float(high.iloc[i - 1]), float(high.iloc[i - 2]))
                if high.iloc[i] > sar:
                    rising = True
                    sar = ep
                    ep = float(high.iloc[i])
                    sar_af = 0.02
                    sar_flip = "BULLISH"
                else:
                    if low.iloc[i] < ep:
                        ep = float(low.iloc[i])
                        sar_af = min(sar_af + 0.02, sar_max_af)
            sar_values[i] = sar
        sar_trend = "BULLISH" if rising else "BEARISH"
    sar_current = float(sar_values[-1]) if _finite(sar_values[-1]) else None
    sar_relation = "UNKNOWN" if sar_current is None else "BELOW_PRICE_BULLISH" if sar_current < last else "ABOVE_PRICE_BEARISH"

    # Fibonacci retracement from the latest two alternating confirmed swing pivots.
    fib_pivots_high = _last_two_pivots(high.to_numpy(dtype=float), "HIGH")
    fib_pivots_low = _last_two_pivots(low.to_numpy(dtype=float), "LOW")
    swing_candidates = sorted(
        [(i, price, "HIGH") for i, price in fib_pivots_high if i >= max(0, len(frame) - 100)]
        + [(i, price, "LOW") for i, price in fib_pivots_low if i >= max(0, len(frame) - 100)],
        key=lambda item: item[0],
    )
    fib = {"status": "INSUFFICIENT_SWING_PIVOTS", "direction": "UNKNOWN", "levels": {}, "nearest_level": None, "swing_low": None, "swing_high": None}
    if len(swing_candidates) >= 2:
        first, second = swing_candidates[-2], swing_candidates[-1]
        if first[2] != second[2] and second[1] != first[1]:
            swing_low, swing_high = sorted((first[1], second[1]))
            impulse = "BULLISH" if first[2] == "LOW" and second[2] == "HIGH" else "BEARISH"
            span = swing_high - swing_low
            ratios = (0.236, 0.382, 0.5, 0.618, 0.786)
            levels = {
                f"{ratio * 100:.1f}%": (swing_high - span * ratio if impulse == "BULLISH" else swing_low + span * ratio)
                for ratio in ratios
            }
            nearest_key, nearest_price = min(levels.items(), key=lambda item: abs(last - item[1]))
            fib = {
                "status": "CALCULATED", "direction": impulse, "swing_low": swing_low, "swing_high": swing_high,
                "levels": levels, "nearest_level": {"level": nearest_key, "price": nearest_price, "distance_pct": abs(last-nearest_price)/last*100},
                "golden_zone": {"lower": min(levels["50.0%"], levels["61.8%"]), "upper": max(levels["50.0%"], levels["61.8%"])},
            }

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
        "macd": {
            "fast_period": 12, "slow_period": 26, "signal_period": 9,
            "line": float(macd_line.iloc[-1]) if _finite(macd_line.iloc[-1]) else None,
            "signal": float(macd_signal.iloc[-1]) if _finite(macd_signal.iloc[-1]) else None,
            "histogram": float(macd_hist.iloc[-1]) if _finite(macd_hist.iloc[-1]) else None,
            "crossover": macd_cross, "zero_line": macd_zero, "divergence": macd_divergence,
        },
        "fibonacci": fib,
        "parabolic_sar": {
            "value": sar_current, "trend": sar_trend, "price_relation": sar_relation, "last_flip": sar_flip,
            "acceleration_start": 0.02, "acceleration_step": 0.02, "acceleration_max": 0.20,
            "trailing_stop_reference": sar_current,
        },
        "atr": {
            "period": 14, "value": atr_value, "percentage": atr_pct_value,
            "risk_references": atr_risk, "directional_signal": "NONE_VOLATILITY_ONLY",
        },
        "rsi": {
            "period": 14, "value": rsi_value, "previous": rsi_previous,
            "state": rsi_state, "midline_50": midline,
            "divergence": rsi_divergence, "trendline_breakout": rsi_trendline,
        },
        "summary_fa": "MA جهت روند، OBV همراهی حجم، RSI و MACD مومنتوم، فیبوناچی نواحی اصلاح، SAR جهت و حد متحرک، و ATR فقط نوسان را می‌سنجند؛ هیچ‌کدام به‌تنهایی مجوز سفارش نیستند.",
    }
