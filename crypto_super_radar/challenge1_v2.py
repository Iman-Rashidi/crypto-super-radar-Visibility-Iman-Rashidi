"""Deterministic Challenge 1 signal engine: 1H regime + 15M entry.

This module is analysis-only. It never submits exchange orders and accepts
only candles whose close time is <= the caller's UTC asof timestamp.
"""
from __future__ import annotations

from typing import Any
import math
import numpy as np
import pandas as pd


def _validate(frame: pd.DataFrame, label: str, minimum: int) -> pd.DataFrame:
    required = {"open", "high", "low", "close", "volume"}
    if not isinstance(frame, pd.DataFrame) or not required.issubset(frame.columns):
        raise ValueError(f"{label} must contain OHLCV columns")
    if not isinstance(frame.index, pd.DatetimeIndex) or frame.index.tz is None:
        raise ValueError(f"{label} index must be timezone-aware")
    data = frame.loc[:, ["open", "high", "low", "close", "volume"]].copy().sort_index()
    if data.index.has_duplicates:
        raise ValueError(f"{label} contains duplicate timestamps")
    data = data.apply(pd.to_numeric, errors="coerce")
    if data.isna().any().any() or not np.isfinite(data.to_numpy(dtype=float)).all():
        raise ValueError(f"{label} contains missing or non-finite values")
    if (data[["open", "high", "low", "close"]] <= 0).any().any() or (data["volume"] < 0).any():
        raise ValueError(f"{label} contains invalid OHLCV values")
    if (data["high"] < data[["open", "close", "low"]].max(axis=1)).any():
        raise ValueError(f"{label} high is inconsistent with OHLC")
    if (data["low"] > data[["open", "close", "high"]].min(axis=1)).any():
        raise ValueError(f"{label} low is inconsistent with OHLC")
    if len(data) < minimum:
        raise ValueError(f"{label} has {len(data)} rows; {minimum} required")
    return data


def _closed(frame: pd.DataFrame, asof: pd.Timestamp, duration: pd.Timedelta) -> pd.DataFrame:
    return frame.loc[(frame.index + duration) <= asof]


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).where(loss != 0, 100.0).where(gain != 0, 0.0)


def true_range(frame: pd.DataFrame) -> pd.Series:
    prev = frame["close"].shift(1)
    return pd.concat([
        frame["high"] - frame["low"],
        (frame["high"] - prev).abs(),
        (frame["low"] - prev).abs(),
    ], axis=1).max(axis=1)


def atr(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    return true_range(frame).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def adx(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    up = frame["high"].diff()
    down = -frame["low"].diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    tr = true_range(frame).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    plus = 100 * plus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / tr.replace(0, np.nan)
    minus = 100 * minus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / tr.replace(0, np.nan)
    dx = 100 * (plus - minus).abs() / (plus + minus).replace(0, np.nan)
    return dx.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def _swing_structure(frame: pd.DataFrame, direction: str, radius: int = 2) -> bool:
    highs, lows = frame["high"].to_numpy(), frame["low"].to_numpy()
    pivot_highs, pivot_lows = [], []
    for i in range(radius, len(frame) - radius):
        neighbours = np.arange(2 * radius + 1) != radius
        local_highs = highs[i - radius:i + radius + 1]
        local_lows = lows[i - radius:i + radius + 1]
        if highs[i] > np.max(local_highs[neighbours]):
            pivot_highs.append(float(highs[i]))
        if lows[i] < np.min(local_lows[neighbours]):
            pivot_lows.append(float(lows[i]))
    if len(pivot_highs) < 2 or len(pivot_lows) < 2:
        return False
    if direction == "LONG":
        return pivot_highs[-1] > pivot_highs[-2] and pivot_lows[-1] > pivot_lows[-2]
    return pivot_highs[-1] < pivot_highs[-2] and pivot_lows[-1] < pivot_lows[-2]


def _regime(frame_1h: pd.DataFrame, frame_4h: pd.DataFrame, asof: pd.Timestamp) -> dict[str, Any]:
    h1 = _closed(_validate(frame_1h, "1H", 205), asof, pd.Timedelta(hours=1))
    h4 = _closed(_validate(frame_4h, "4H", 205), asof, pd.Timedelta(hours=4))
    if len(h1) < 205:
        return {"status": "UNKNOWN", "direction": "NONE", "reason": "1H closed-candle history is insufficient"}
    close = h1["close"]
    e21, e55, e200 = ema(close, 21), ema(close, 55), ema(close, 200)
    adx14 = adx(h1, 14)
    row = h1.iloc[-1]
    vals = [e21.iloc[-1], e55.iloc[-1], e200.iloc[-1], adx14.iloc[-1]]
    if any(pd.isna(x) or not math.isfinite(float(x)) for x in vals):
        return {"status": "UNKNOWN", "direction": "NONE", "reason": "1H indicator warm-up incomplete"}
    strength = float(adx14.iloc[-1])
    if strength < 22:
        return {"status": "RANGE", "direction": "NONE", "adx": strength, "reason": "ADX(14) below 22"}
    # Require both fast and slow trend EMAs to slope with the regime.
    # Their ordering alone can persist after momentum has already rolled over.
    ema21_slope = float(e21.iloc[-1] - e21.iloc[-4])
    ema55_slope = float(e55.iloc[-1] - e55.iloc[-4])
    long_ok = (
        row["close"] > e200.iloc[-1]
        and e21.iloc[-1] > e55.iloc[-1]
        and ema21_slope > 0
        and ema55_slope > 0
        and _swing_structure(h1, "LONG")
    )
    short_ok = (
        row["close"] < e200.iloc[-1]
        and e21.iloc[-1] < e55.iloc[-1]
        and ema21_slope < 0
        and ema55_slope < 0
        and _swing_structure(h1, "SHORT")
    )
    direction = "LONG" if long_ok else "SHORT" if short_ok else "NONE"
    aligned_4h = False
    if len(h4) >= 205:
        c4 = h4["close"]
        e50_4h, e200_4h = ema(c4, 50), ema(c4, 200)
        if direction == "LONG" and not pd.isna(e50_4h.iloc[-4]) and not pd.isna(e200_4h.iloc[-1]):
            aligned_4h = c4.iloc[-1] > e200_4h.iloc[-1] and e50_4h.iloc[-1] > e50_4h.iloc[-4]
        elif direction == "SHORT" and not pd.isna(e50_4h.iloc[-4]) and not pd.isna(e200_4h.iloc[-1]):
            aligned_4h = c4.iloc[-1] < e200_4h.iloc[-1] and e50_4h.iloc[-1] < e50_4h.iloc[-4]
    return {
        "status": "TREND" if direction != "NONE" else "NEUTRAL", "direction": direction,
        "adx_1h": strength, "close_1h": float(row["close"]),
        "ema21_1h": float(e21.iloc[-1]), "ema55_1h": float(e55.iloc[-1]),
        "ema200_1h": float(e200.iloc[-1]),
        "ema21_slope_1h": ema21_slope,
        "ema55_slope_1h": ema55_slope,
        "aligned_4h": bool(aligned_4h),
        "candle_open_utc": h1.index[-1].isoformat(),
        "reason": "1H trend, EMA structure and HH/HL or LL/LH hard gate",
    }


def analyze_signal(
    *, instrument_id: str, frame_1h: pd.DataFrame, frame_4h: pd.DataFrame,
    frame_15m: pd.DataFrame, btc_15m: pd.DataFrame, asof: pd.Timestamp,
    funding_rate: float | None, spread_fraction: float | None,
    last_three_results: tuple[int, int, int] = (-1, -1, -1),
    tp_r: float = 2.4,
) -> dict[str, Any]:
    """Return a completed-candle decision plus ML feature vector source values."""
    if asof.tzinfo is None:
        raise ValueError("asof must be timezone-aware")
    asof = asof.tz_convert("UTC")
    symbol = str(instrument_id).upper()
    regime = _regime(frame_1h, frame_4h, asof)
    if regime["direction"] not in {"LONG", "SHORT"}:
        return {"signal": "NO_TRADE", "reason": regime.get("reason"), "regime": regime}
    if funding_rate is None or spread_fraction is None:
        return {"signal": "NO_TRADE", "reason": "Funding or spread estimate is missing; no zero assumption", "regime": regime}
    funding = float(funding_rate)
    spread = float(spread_fraction)
    if not math.isfinite(funding) or not math.isfinite(spread) or spread < 0 or spread > 0.0004:
        return {"signal": "NO_TRADE", "reason": "Funding/spread guard failed", "regime": regime}
    direction = regime["direction"]
    if (direction == "LONG" and funding > 0.0005) or (direction == "SHORT" and funding < -0.0005):
        return {"signal": "NO_TRADE", "reason": "Funding crowding guard blocked entry", "regime": regime}

    m15 = _closed(_validate(frame_15m, "15M", 70), asof, pd.Timedelta(minutes=15))
    if len(m15) < 60:
        return {"signal": "NO_TRADE", "reason": "15M history insufficient", "regime": regime}
    close = m15["close"]
    e21, e55 = ema(close, 21), ema(close, 55)
    rsi14, atr14 = rsi(close, 14), atr(m15, 14)
    vol_sma = m15["volume"].rolling(20, min_periods=20).mean()
    last, previous = m15.iloc[-1], m15.iloc[-2]
    vals = [e21.iloc[-1], e55.iloc[-1], rsi14.iloc[-1], atr14.iloc[-1], vol_sma.iloc[-1]]
    if any(pd.isna(x) or not math.isfinite(float(x)) for x in vals):
        return {"signal": "NO_TRADE", "reason": "15M indicator warm-up incomplete", "regime": regime}
    low_band, high_band = sorted([float(e21.iloc[-1]), float(e55.iloc[-1])])
    value_zone = float(last["low"]) <= high_band and float(last["high"]) >= low_band
    rsi_value = float(rsi14.iloc[-1])
    volume_ratio = float(last["volume"] / vol_sma.iloc[-1]) if vol_sma.iloc[-1] > 0 else 0.0
    if direction == "LONG":
        trigger = float(last["close"]) > float(previous["high"]) and float(last["close"]) > float(last["open"])
    else:
        trigger = float(last["close"]) < float(previous["low"]) and float(last["close"]) < float(last["open"])
    if not value_zone or not 40 <= rsi_value <= 60 or not trigger or volume_ratio < 1.2:
        return {"signal": "NO_TRADE", "reason": "15M value zone, RSI, close-break or 1.2x volume hard gate failed", "regime": regime,
                "setup": {"value_zone": value_zone, "rsi_15m": rsi_value, "volume_ratio": volume_ratio, "trigger": trigger}}

    atr_series = atr14.dropna()
    if len(atr_series) < 25:
        return {"signal": "NO_TRADE", "reason": "ATR shock baseline unavailable", "regime": regime}
    prior_mean = float(atr_series.iloc[-21:-1].mean())
    current_atr = float(atr_series.iloc[-1])
    if prior_mean <= 0 or current_atr > 2.5 * prior_mean:
        return {"signal": "NO_TRADE", "reason": "15M ATR shock exceeds 2.5x its 20-bar baseline", "regime": regime}

    btc = _closed(_validate(btc_15m, "BTC 15M", 40), asof, pd.Timedelta(minutes=15))
    if len(btc) < 25:
        return {"signal": "NO_TRADE", "reason": "BTC master feed unavailable", "regime": regime}
    btc_ema21 = ema(btc["close"], 21)
    btc_change = float(btc["close"].iloc[-1] / btc["close"].iloc[-2] - 1.0)
    if symbol != "BTC-USDT":
        btc_ok = (float(btc["close"].iloc[-1]) > float(btc_ema21.iloc[-1]) and btc_change > -0.008) if direction == "LONG" else (
            float(btc["close"].iloc[-1]) < float(btc_ema21.iloc[-1]) and btc_change < 0.008
        )
    else:
        btc_ok = True
    if not btc_ok:
        return {"signal": "NO_TRADE", "reason": "BTC master guard blocked altcoin setup", "regime": regime}

    entry = float(last["close"])
    atr_mult = 1.3 if symbol in {"BTC-USDT", "ETH-USDT"} else 1.8
    if direction == "LONG":
        stop = min(float(m15["low"].tail(5).min()), entry - atr_mult * current_atr)
        target = entry + tp_r * (entry - stop)
    else:
        stop = max(float(m15["high"].tail(5).max()), entry + atr_mult * current_atr)
        target = entry - tp_r * (stop - entry)
    stop_pct = abs(entry - stop) / entry
    if stop_pct <= 0 or stop_pct > 0.015:
        return {"signal": "NO_TRADE", "reason": "stop distance breaches 1.5% volatility cap", "regime": regime}

    score_parts = {
        "h1_h4_alignment": 30 if regime.get("aligned_4h") else 0,
        "adx_gt_28": 20 if float(regime["adx_1h"]) > 28 else 0,
        "value_zone_rejection": 25,
        "volume_gt_1_5x": 15 if volume_ratio >= 1.5 else 0,
        "btc_alignment": 10 if btc_ok else 0,
    }
    confidence_score = sum(score_parts.values())
    if confidence_score < 70:
        return {"signal": "NO_TRADE", "reason": "confluence score below 70", "regime": regime,
                "confidence_score": confidence_score, "score_parts": score_parts}
    hour = m15.index[-1].hour
    ema200_1h = float(regime["ema200_1h"])
    features = {
        "adx_1h": float(regime["adx_1h"]),
        "rsi_15m": rsi_value,
        "ema200_distance_pct": (float(last["close"]) / ema200_1h - 1.0) * 100.0,
        "funding_rate_pct": funding * 100.0,
        "spread_pct": spread * 100.0,
        "volume_ratio": volume_ratio,
        "utc_hour_sin": math.sin(2 * math.pi * hour / 24),
        "utc_hour_cos": math.cos(2 * math.pi * hour / 24),
        "last_trade_1_win": int(last_three_results[0]),
        "last_trade_2_win": int(last_three_results[1]),
        "last_trade_3_win": int(last_three_results[2]),
    }
    signal_time = m15.index[-1].isoformat()
    signal_id = f"challenge1-v2:{symbol}:15m:{signal_time}:{direction}"
    return {
        "signal": direction, "signal_id": signal_id, "instrument_id": symbol,
        "entry": entry, "stop_loss": stop, "take_profit": target,
        "atr_15m": current_atr, "gross_rr": abs(target - entry) / abs(entry - stop),
        "confidence_score": confidence_score, "score_parts": score_parts,
        "regime": regime, "volume_ratio": volume_ratio, "rsi_15m": rsi_value,
        "funding_rate": funding, "spread_fraction": spread,
        "signal_candle_open_utc": signal_time, "features": features,
        "orders_submitted": False,
    }
