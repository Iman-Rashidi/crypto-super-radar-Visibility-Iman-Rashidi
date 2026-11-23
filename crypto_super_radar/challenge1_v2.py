"""Deterministic Challenge 1 signal engine: 1H regime + 15M entry.

This module is analysis-only. It never submits exchange orders and accepts
only candles whose close time is <= the caller's UTC asof timestamp.
"""
from __future__ import annotations

from typing import Any, Mapping
import math
import numpy as np
import pandas as pd
from .elliott_waves import monitor_elliott_waves
from .challenge1_strategy import challenge1_entry_evidence_ready

MAX_STOP_DISTANCE_PCT = 0.035


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


def _closed_count(frame: pd.DataFrame, asof: pd.Timestamp, duration: pd.Timedelta) -> int:
    """Count only candles whose close timestamp is no later than the UTC cutoff."""
    cutoff = asof.tz_convert("UTC") - duration
    return int(frame.index.searchsorted(cutoff, side="right"))


def _closed_prefix(
    frame: pd.DataFrame, asof: pd.Timestamp, duration: pd.Timedelta,
) -> tuple[pd.DataFrame, int]:
    """Return a zero-copy positional prefix of fully closed candles."""
    end = _closed_count(frame, asof, duration)
    return frame.iloc[:end], end


def _precompute_swing_pivots(frame: pd.DataFrame, radius: int = 2) -> dict[str, np.ndarray]:
    """Precompute confirmed local pivots with the exact legacy comparison rule."""
    highs = frame["high"].to_numpy(dtype=float)
    lows = frame["low"].to_numpy(dtype=float)
    high_values: list[float] = []
    high_confirmed_at: list[int] = []
    low_values: list[float] = []
    low_confirmed_at: list[int] = []
    neighbours = np.arange(2 * radius + 1) != radius
    for center in range(radius, len(frame) - radius):
        local_highs = highs[center - radius:center + radius + 1]
        local_lows = lows[center - radius:center + radius + 1]
        if highs[center] > np.max(local_highs[neighbours]):
            high_values.append(float(highs[center]))
            high_confirmed_at.append(center + radius)
        if lows[center] < np.min(local_lows[neighbours]):
            low_values.append(float(lows[center]))
            low_confirmed_at.append(center + radius)
    return {
        "high_values": np.asarray(high_values, dtype=float),
        "high_confirmed_at": np.asarray(high_confirmed_at, dtype=np.int64),
        "low_values": np.asarray(low_values, dtype=float),
        "low_confirmed_at": np.asarray(low_confirmed_at, dtype=np.int64),
    }


def _cached_swing_structure(
    pivots: Mapping[str, np.ndarray], visible_rows: int, direction: str,
) -> bool:
    """Use only pivots whose right-hand confirmation candles are closed as-of."""
    last_visible_index = int(visible_rows) - 1
    high_end = int(np.searchsorted(
        pivots["high_confirmed_at"], last_visible_index, side="right"
    ))
    low_end = int(np.searchsorted(
        pivots["low_confirmed_at"], last_visible_index, side="right"
    ))
    pivot_highs = pivots["high_values"][:high_end]
    pivot_lows = pivots["low_values"][:low_end]
    if len(pivot_highs) < 2 or len(pivot_lows) < 2:
        return False
    if direction == "LONG":
        return pivot_highs[-1] > pivot_highs[-2] and pivot_lows[-1] > pivot_lows[-2]
    return pivot_highs[-1] < pivot_highs[-2] and pivot_lows[-1] < pivot_lows[-2]


def prepare_signal_context(
    frame_1h: pd.DataFrame,
    frame_4h: pd.DataFrame,
    frame_15m: pd.DataFrame,
    btc_15m: pd.DataFrame,
) -> dict[str, Any]:
    """Validate once and precompute causal indicators for the offline walk-forward loop.

    Pandas EWM and rolling values at index t depend only on rows <= t. Swing
    pivots additionally store their confirmation row, preventing a pivot from
    becoming visible before its complete right-hand confirmation window.
    """
    frames = {
        "1H": _validate(frame_1h, "1H", 205),
        "4H": _validate(frame_4h, "4H", 205),
        "15M": _validate(frame_15m, "15M", 70),
        "BTC_15M": _validate(btc_15m, "BTC 15M", 40),
    }
    h1, h4, m15, btc = (
        frames["1H"], frames["4H"], frames["15M"], frames["BTC_15M"]
    )
    atr_15m = atr(m15, 14)
    indicators = {
        "1H": {
            "ema21": ema(h1["close"], 21),
            "ema55": ema(h1["close"], 55),
            "ema200": ema(h1["close"], 200),
            "adx14": adx(h1, 14),
        },
        "4H": {
            "ema50": ema(h4["close"], 50),
            "ema200": ema(h4["close"], 200),
        },
        "15M": {
            "ema21": ema(m15["close"], 21),
            "ema55": ema(m15["close"], 55),
            "rsi14": rsi(m15["close"], 14),
            "atr14": atr_15m,
            "atr14_valid": atr_15m.dropna(),
            "volume_sma20": m15["volume"].rolling(20, min_periods=20).mean(),
        },
        "BTC_15M": {
            "ema21": ema(btc["close"], 21),
        },
    }
    return {
        "version": 1,
        "frames": frames,
        "indicators": indicators,
        "swings": {"1H": _precompute_swing_pivots(h1)},
    }


def prepared_signal_history_ready(
    context: Mapping[str, Any], asof: pd.Timestamp,
) -> bool:
    """Fast equivalent of the legacy closed-history warm-up checks."""
    frames = context["frames"]
    return (
        _closed_count(frames["1H"], asof, pd.Timedelta(hours=1)) >= 205
        and _closed_count(frames["4H"], asof, pd.Timedelta(hours=4)) >= 205
        and _closed_count(frames["15M"], asof, pd.Timedelta(minutes=15)) >= 70
        and _closed_count(frames["BTC_15M"], asof, pd.Timedelta(minutes=15)) >= 40
    )


def _regime(
    frame_1h: pd.DataFrame,
    frame_4h: pd.DataFrame,
    asof: pd.Timestamp,
    *,
    precomputed_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if precomputed_context is None:
        h1 = _closed(_validate(frame_1h, "1H", 205), asof, pd.Timedelta(hours=1))
        h4 = _closed(_validate(frame_4h, "4H", 205), asof, pd.Timedelta(hours=4))
        h1_end, h4_end = len(h1), len(h4)
    else:
        frames = precomputed_context["frames"]
        h1, h1_end = _closed_prefix(frames["1H"], asof, pd.Timedelta(hours=1))
        h4, h4_end = _closed_prefix(frames["4H"], asof, pd.Timedelta(hours=4))
    if len(h1) < 205:
        return {"status": "UNKNOWN", "direction": "NONE", "reason": "1H closed-candle history is insufficient"}
    close = h1["close"]
    if precomputed_context is None:
        e21, e55, e200 = ema(close, 21), ema(close, 55), ema(close, 200)
        adx14 = adx(h1, 14)
        swing_long = _swing_structure(h1, "LONG")
        swing_short = _swing_structure(h1, "SHORT")
    else:
        h1_indicators = precomputed_context["indicators"]["1H"]
        e21 = h1_indicators["ema21"].iloc[:h1_end]
        e55 = h1_indicators["ema55"].iloc[:h1_end]
        e200 = h1_indicators["ema200"].iloc[:h1_end]
        adx14 = h1_indicators["adx14"].iloc[:h1_end]
        swing_cache = precomputed_context["swings"]["1H"]
        swing_long = _cached_swing_structure(swing_cache, h1_end, "LONG")
        swing_short = _cached_swing_structure(swing_cache, h1_end, "SHORT")
    row = h1.iloc[-1]
    vals = [e21.iloc[-1], e55.iloc[-1], e200.iloc[-1], adx14.iloc[-1]]
    if any(pd.isna(x) or not math.isfinite(float(x)) for x in vals):
        return {"status": "UNKNOWN", "direction": "NONE", "reason": "1H indicator warm-up incomplete"}
    strength = float(adx14.iloc[-1])
    if strength < 22:
        return {"status": "RANGE", "direction": "NONE", "adx": strength, "reason": "ADX(14) below 22"}
    # ADX confirms trend strength; close location versus both fast EMAs sets direction.
    # HH/HL or LL/LH remains measurable confluence, never a standalone hard gate.
    ema21_slope = float(e21.iloc[-1] - e21.iloc[-4])
    ema55_slope = float(e55.iloc[-1] - e55.iloc[-4])
    # Direction is decided on the completed 1H candle only: ADX(14) >= 22
    # plus close vs EMA21. EMA55 and swing structure remain descriptive context,
    # not entry gates; 15m is reserved for entry price and protective-stop sizing.
    long_ok = float(row["close"]) > float(e21.iloc[-1])
    short_ok = float(row["close"]) < float(e21.iloc[-1])
    direction = "LONG" if long_ok else "SHORT" if short_ok else "NONE"
    structure_aligned = bool(swing_long if direction == "LONG" else swing_short if direction == "SHORT" else False)
    aligned_4h = False
    opposed_4h = False
    neutral_4h = True
    if len(h4) >= 205:
        c4 = h4["close"]
        if precomputed_context is None:
            e50_4h, e200_4h = ema(c4, 50), ema(c4, 200)
        else:
            h4_indicators = precomputed_context["indicators"]["4H"]
            e50_4h = h4_indicators["ema50"].iloc[:h4_end]
            e200_4h = h4_indicators["ema200"].iloc[:h4_end]
        if direction in {"LONG", "SHORT"} and not pd.isna(e50_4h.iloc[-4]) and not pd.isna(e200_4h.iloc[-1]):
            h4_bearish = c4.iloc[-1] < e200_4h.iloc[-1] and e50_4h.iloc[-1] < e50_4h.iloc[-4]
            h4_bullish = c4.iloc[-1] > e200_4h.iloc[-1] and e50_4h.iloc[-1] > e50_4h.iloc[-4]
            aligned_4h = bool(h4_bullish if direction == "LONG" else h4_bearish)
            opposed_4h = bool(h4_bearish if direction == "LONG" else h4_bullish)
            neutral_4h = not aligned_4h and not opposed_4h
    return {
        "status": "TREND" if direction != "NONE" else "NEUTRAL", "direction": direction,
        "adx_1h": strength, "close_1h": float(row["close"]),
        "ema21_1h": float(e21.iloc[-1]), "ema55_1h": float(e55.iloc[-1]),
        "ema200_1h": float(e200.iloc[-1]),
        "ema21_slope_1h": ema21_slope,
        "ema55_slope_1h": ema55_slope,
        "aligned_4h": bool(aligned_4h),
        "opposed_4h": bool(opposed_4h),
        "neutral_4h": bool(neutral_4h),
        "structure_aligned_1h": structure_aligned,
        "candle_open_utc": h1.index[-1].isoformat(),
        "candle_open_utc_4h": h4.index[-1].isoformat() if not h4.empty else None,
        "reason": "ADX(14)>=22 and completed 1H close above/below EMA21; EMA55 and swing structure are informational",
    }

def _elliott_wave_1h_evidence(
    frame_1h: pd.DataFrame,
    asof: pd.Timestamp,
    *,
    expected_direction: str,
    precomputed_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Capture structural wave evidence from completed 1H candles only.

    This is evidence metadata, not a direction/probability override. The V2
    strategy's existing signal gates and score remain unchanged.
    """
    if precomputed_context is None:
        closed = _closed(
            _validate(frame_1h, "1H", 205),
            asof,
            pd.Timedelta(hours=1),
        )
    else:
        closed, _ = _closed_prefix(
            precomputed_context["frames"]["1H"], asof, pd.Timedelta(hours=1)
        )
    wave = monitor_elliott_waves(closed)
    pattern = str(wave.get("pattern") or "AMBIGUOUS").upper()
    phase = str(wave.get("phase") or "INSUFFICIENT_DATA").upper()
    allowed_phases = {
        "WAVES_1_AND_2_CONFIRMED",
        "WAVE_3_CONFIRMED",
        "WAVE_4_CONFIRMED",
        "FIVE_WAVE_IMPULSE_CONFIRMED",
    }
    expected = str(expected_direction or "").strip().upper()
    if expected not in {"LONG", "SHORT"}:
        raise ValueError("expected_direction must be LONG or SHORT")
    expected_pattern = "BULLISH" if expected == "LONG" else "BEARISH"
    if pattern in {"BULLISH", "BEARISH"} and phase in allowed_phases:
        wave_alignment = "ALIGNED" if pattern == expected_pattern else "OPPOSED"
    else:
        wave_alignment = "UNCONFIRMED"
    return {
        **wave,
        "timeframe": "1H",
        "evidence_source": "CONFIRMED_CLOSED_1H_CANDLES",
        "expected_signal_direction": expected,
        "latest_closed_candle_open_utc": (
            closed.index[-1].isoformat() if not closed.empty else None
        ),
        "data_asof_utc": asof.isoformat(),
        "wave_alignment": wave_alignment,
    }


EARLY_W3_MAX_BREAKOUT_PCT = 0.012
EARLY_W3_ATR_MULTIPLE = 1.5
EARLY_W3_STOP_BUFFER_ATR = 0.10



RSI_DIVERGENCE_PIVOT_REVERSAL = 0.002
BREAKOUT_VOLUME_MULTIPLE = 1.20


def detect_confirmed_rsi_divergence(frame: pd.DataFrame, *, timeframe: str) -> dict[str, Any]:
    """Detect regular RSI divergence only at confirmed Elliott pivots."""
    wave = monitor_elliott_waves(frame, min_reversal_pct=0.002, max_reversal_pct=0.03)
    pivots = wave.get("pivots") if isinstance(wave.get("pivots"), list) else []
    rsi_values = rsi(frame["close"], 14)
    highs = [p for p in pivots if str(p.get("kind", "")).upper() == "HIGH"]
    lows = [p for p in pivots if str(p.get("kind", "")).upper() == "LOW"]

    def pair(items: list[dict[str, Any]], kind: str) -> dict[str, Any]:
        if len(items) < 2:
            return {"type": "NONE", "timeframe": timeframe, "reason": "دو پیوت تأییدشده برای مقایسه وجود ندارد."}
        left, right = items[-2:]
        try:
            i1, i2 = int(left["index"]), int(right["index"])
            p1, p2 = float(left["price"]), float(right["price"])
            r1, r2 = float(rsi_values.iloc[i1]), float(rsi_values.iloc[i2])
        except (KeyError, TypeError, ValueError, IndexError, OverflowError):
            return {"type": "UNKNOWN", "timeframe": timeframe, "reason": "داده پیوت یا RSI معتبر نیست."}
        if not all(math.isfinite(x) for x in (p1, p2, r1, r2)):
            return {"type": "UNKNOWN", "timeframe": timeframe, "reason": "مقدار RSI یا قیمت پیوت معتبر نیست."}
        divergence = (
            "BEARISH" if kind == "HIGH" and p2 > p1 and r2 < r1
            else "BULLISH" if kind == "LOW" and p2 < p1 and r2 > r1
            else "NONE"
        )
        return {
            "type": divergence, "timeframe": timeframe,
            "price_pivot_1": p1, "price_pivot_2": p2,
            "rsi_pivot_1": r1, "rsi_pivot_2": r2,
            "pivot_1_confirmed_at": left.get("confirmed_at"),
            "pivot_2_confirmed_at": right.get("confirmed_at"),
            "confirmed_only": True,
        }

    bearish = pair(highs, "HIGH")
    bullish = pair(lows, "LOW")
    if bearish.get("type") == "BEARISH":
        return bearish
    if bullish.get("type") == "BULLISH":
        return bullish
    if "UNKNOWN" in {bearish.get("type"), bullish.get("type")}:
        return {"type": "UNKNOWN", "timeframe": timeframe, "details": [bearish, bullish]}
    return {"type": "NONE", "timeframe": timeframe, "details": [bearish, bullish]}


def rsi_divergence_entry_guard(frame_5m: pd.DataFrame, frame_15m: pd.DataFrame, side: str) -> dict[str, Any]:
    """Fail closed: bearish divergence blocks LONG; bullish divergence blocks SHORT."""
    checks = [
        detect_confirmed_rsi_divergence(frame_5m, timeframe="5m"),
        detect_confirmed_rsi_divergence(frame_15m, timeframe="15m"),
    ]
    direction = str(side or "").upper()
    blocked = any(
        (direction == "LONG" and item.get("type") == "BEARISH")
        or (direction == "SHORT" and item.get("type") == "BULLISH")
        for item in checks
    )
    unknown = any(item.get("type") == "UNKNOWN" for item in checks)
    return {
        "blocked": bool(blocked),
        "reason": (
            "RSI_DIVERGENCE_OPPOSES_ENTRY" if blocked else
            "RSI_DIVERGENCE_EVIDENCE_UNKNOWN" if unknown else
            "NO_OPPOSING_CONFIRMED_RSI_DIVERGENCE"
        ),
        "timeframes": checks,
    }


def breakout_volume_confirmation(frame: pd.DataFrame, *, multiple: float = BREAKOUT_VOLUME_MULTIPLE) -> dict[str, Any]:
    """Require the latest closed candle volume to exceed prior 20-candle mean."""
    if len(frame) < 21:
        return {"confirmed": False, "reason": "Insufficient 20-candle volume baseline"}
    baseline = float(frame["volume"].iloc[-21:-1].mean())
    current = float(frame["volume"].iloc[-1])
    ratio = current / baseline if baseline > 0 else math.inf
    return {
        "confirmed": bool(math.isfinite(ratio) and ratio >= float(multiple)),
        "current_volume": current, "average_volume_20": baseline,
        "volume_ratio": ratio if math.isfinite(ratio) else None,
        "required_multiple": float(multiple),
        "reason": "VOLUME_CONFIRMED" if math.isfinite(ratio) and ratio >= float(multiple) else "BREAKOUT_VOLUME_TOO_LOW",
    }


def calculate_wave4_fibonacci(frame_5m: pd.DataFrame) -> dict[str, Any]:
    """Calculate W3 retracement levels only from a confirmed five-pivot W4 structure."""
    wave = monitor_elliott_waves(frame_5m, min_reversal_pct=0.002, max_reversal_pct=0.03)
    pivots = wave.get("pivots") if isinstance(wave.get("pivots"), list) else []
    if str(wave.get("phase", "")).upper() != "WAVE_4_CONFIRMED" or len(pivots) < 5:
        return {"status": "UNCONFIRMED", "timeframe": "5m", "levels": {}}
    points = pivots[-5:]
    pattern = str(wave.get("pattern") or "").upper()
    expected = ["LOW", "HIGH", "LOW", "HIGH", "LOW"] if pattern == "BULLISH" else ["HIGH", "LOW", "HIGH", "LOW", "HIGH"]
    if pattern not in {"BULLISH", "BEARISH"} or [str(p.get("kind", "")).upper() for p in points] != expected:
        return {"status": "UNCONFIRMED", "timeframe": "5m", "levels": {}}
    w3_start, w3_end = float(points[2]["price"]), float(points[3]["price"])
    length = abs(w3_end - w3_start)
    if length <= 0:
        return {"status": "INVALID", "timeframe": "5m", "levels": {}}
    sign = -1.0 if pattern == "BULLISH" else 1.0
    levels = {str(level): w3_end + sign * level * length for level in (0.382, 0.5, 0.618)}
    return {
        "status": "CONFIRMED", "timeframe": "5m", "pattern": pattern,
        "w3_start": w3_start, "w3_end": w3_end,
        "w4_pivot": float(points[4]["price"]), "levels": levels,
        "pivot_confirmed_at": points[4].get("confirmed_at"),
        "completed_pivots_only": True,
    }


def detect_early_wave3_breakout(
    frame_5m: pd.DataFrame, frame_15m: pd.DataFrame, *,
    asof: pd.Timestamp, entry_price: float,
) -> dict[str, Any]:
    """Detect a just-confirmed W1 breakout after W2 on closed 5m/15m candles.

    The pivot monitor reports pivots only after reversal confirmation. The
    breakout must occur on the latest fully closed candle; no intrabar lookahead.
    """
    if asof.tzinfo is None:
        raise ValueError("asof must be timezone-aware")
    asof = asof.tz_convert("UTC")
    five = _closed(_validate(frame_5m, "5M", 30), asof, pd.Timedelta(minutes=5))
    fifteen = _closed(_validate(frame_15m, "15M", 30), asof, pd.Timedelta(minutes=15))
    if len(five) < 30 or len(fifteen) < 30:
        return {"signal": "NO_TRADE", "reason": "Insufficient closed 5m/15m candles"}
    if not math.isfinite(float(entry_price)) or float(entry_price) <= 0:
        return {"signal": "NO_TRADE", "reason": "Invalid executable quote"}

    # Evaluate the same rule-based dashboard wave engine at both fast intervals.
    wave5 = monitor_elliott_waves(five, min_reversal_pct=0.002, max_reversal_pct=0.03)
    wave15 = monitor_elliott_waves(fifteen, min_reversal_pct=0.002, max_reversal_pct=0.03)
    side = "LONG" if str(wave5.get("pattern", "")).upper() == "BULLISH" else (
        "SHORT" if str(wave5.get("pattern", "")).upper() == "BEARISH" else "NONE"
    )
    if side == "NONE" or str(wave5.get("phase", "")).upper() != "WAVES_1_AND_2_CONFIRMED":
        return {"signal": "NO_TRADE", "reason": "5m wave 1 and wave 2 are not structurally confirmed"}
    expected_kinds = ["LOW", "HIGH", "LOW"] if side == "LONG" else ["HIGH", "LOW", "HIGH"]
    pivots = wave5.get("pivots")
    if not isinstance(pivots, list) or len(pivots) < 3:
        return {"signal": "NO_TRADE", "reason": "Confirmed W1/W2 pivots are missing"}
    points = pivots[-3:]
    if [str(p.get("kind", "")).upper() for p in points] != expected_kinds:
        return {"signal": "NO_TRADE", "reason": "W1/W2 pivot order is invalid"}
    try:
        w1_start, w1_end, w2_end = (float(p["price"]) for p in points)
        pivot_confirmed = [_utc_timestamp(p.get("confirmed_at")) for p in points]
        pivot_times = [_utc_timestamp(p.get("timestamp")) for p in points]
    except (KeyError, TypeError, ValueError, OverflowError):
        return {"signal": "NO_TRADE", "reason": "W1/W2 pivot values are invalid"}
    if any(x is None for x in pivot_confirmed + pivot_times):
        return {"signal": "NO_TRADE", "reason": "W1/W2 pivot timestamps are invalid"}
    if any(t > asof for t in pivot_confirmed) or any(t > c for t, c in zip(pivot_times, pivot_confirmed)):
        return {"signal": "NO_TRADE", "reason": "Wave pivots are not confirmed as of the decision time"}

    close = float(five["close"].iloc[-1])
    previous_close = float(five["close"].iloc[-2])
    w1_level = w1_end
    crossed = previous_close <= w1_level < close if side == "LONG" else previous_close >= w1_level > close
    if not crossed:
        return {"signal": "NO_TRADE", "reason": "Latest closed 5m candle did not initiate the W1 breakout"}
    divergence_guard = rsi_divergence_entry_guard(five, fifteen, side)
    if divergence_guard["blocked"]:
        return {"signal": "NO_TRADE", "reason": divergence_guard["reason"], "rsi_divergence_guard": divergence_guard}
    volume_confirmation = breakout_volume_confirmation(five)
    if not volume_confirmation["confirmed"]:
        return {"signal": "NO_TRADE", "reason": "W3 breakout rejected: " + volume_confirmation["reason"], "volume_confirmation": volume_confirmation}
    atr_value = float(atr(five, 14).iloc[-1])
    rsi_value = float(rsi(five["close"], 14).iloc[-1])
    if not math.isfinite(atr_value) or atr_value <= 0 or not math.isfinite(rsi_value):
        return {"signal": "NO_TRADE", "reason": "5m ATR/RSI is unavailable"}
    distance = (float(entry_price) - w1_level) / w1_level if side == "LONG" else (w1_level - float(entry_price)) / w1_level
    max_extension = min(EARLY_W3_MAX_BREAKOUT_PCT, EARLY_W3_ATR_MULTIPLE * atr_value / w1_level)
    if distance < 0 or distance > max_extension:
        return {"signal": "NO_TRADE", "reason": "Anti-top-chase guard: W3 breakout is already extended"}
    if (side == "LONG" and rsi_value > 78.0) or (side == "SHORT" and rsi_value < 22.0):
        return {"signal": "NO_TRADE", "reason": "Anti-top-chase guard: RSI is in extreme territory"}

    # 15m must independently agree by completed EMA21 direction/slope.
    ema21_15 = ema(fifteen["close"], 21)
    if pd.isna(ema21_15.iloc[-1]) or pd.isna(ema21_15.iloc[-4]):
        return {"signal": "NO_TRADE", "reason": "15m EMA21 alignment is unavailable"}
    m15_close = float(fifteen["close"].iloc[-1])
    m15_slope = float(ema21_15.iloc[-1] - ema21_15.iloc[-4])
    aligned = m15_close > float(ema21_15.iloc[-1]) and m15_slope > 0 if side == "LONG" else (
        m15_close < float(ema21_15.iloc[-1]) and m15_slope < 0
    )
    if not aligned:
        return {"signal": "NO_TRADE", "reason": "15m completed-candle trend conflicts with W3 direction"}
    wave15_pattern = str(wave15.get("pattern") or "AMBIGUOUS").upper()
    if wave15_pattern in {"BULLISH", "BEARISH"} and wave15_pattern != ("BULLISH" if side == "LONG" else "BEARISH"):
        return {"signal": "NO_TRADE", "reason": "15m Elliott structure opposes the 5m W3 breakout"}

    buffer = EARLY_W3_STOP_BUFFER_ATR * atr_value
    stop = w2_end - buffer if side == "LONG" else w2_end + buffer
    risk = float(entry_price) - stop if side == "LONG" else stop - float(entry_price)
    if risk <= 0 or risk / float(entry_price) > MAX_STOP_DISTANCE_PCT:
        return {"signal": "NO_TRADE", "reason": "W2 structural stop is invalid or exceeds 3.5%"}
    w1_length = abs(w1_end - w1_start)
    fib_target = w2_end + 1.618 * w1_length if side == "LONG" else w2_end - 1.618 * w1_length
    rr_target = float(entry_price) + 1.5 * risk if side == "LONG" else float(entry_price) - 1.5 * risk
    target = max(fib_target, rr_target) if side == "LONG" else min(fib_target, rr_target)
    if target <= 0 or (side == "LONG" and not stop < float(entry_price) < target) or (
        side == "SHORT" and not target < float(entry_price) < stop
    ):
        return {"signal": "NO_TRADE", "reason": "W3 Fibonacci/1.5R target is invalid"}

    return {
        "signal": side, "reason": "EARLY_W3_BREAKOUT_CONFIRMED",
        "timeframe": "5m", "entry": float(entry_price), "stop_loss": float(stop),
        "take_profit": float(target), "risk_reward": abs(target - float(entry_price)) / risk,
        "w1_start": w1_start, "w1_end": w1_end, "w2_end": w2_end,
        "atr_5m": atr_value, "rsi_5m": rsi_value,
        "breakout_distance_pct": distance, "max_extension_pct": max_extension,
        "wave_5m": wave5, "wave_15m": wave15,
        "rsi_divergence_guard": divergence_guard,
        "volume_confirmation": volume_confirmation,
        "wave4_fibonacci": calculate_wave4_fibonacci(five),
        "signal_candle_open_utc": five.index[-1].isoformat(),
        "context_15m_candle_open_utc": fifteen.index[-1].isoformat(),
        "asof_utc": asof.isoformat(),
        "completed_candles_only": True,
    }


def build_early_wave3_signal(
    *, instrument_id: str, frame_5m: pd.DataFrame, frame_15m: pd.DataFrame,
    asof: pd.Timestamp, entry_price: float, funding_rate: float,
    funding_kind: str, funding_event_time_ms: int, funding_max_age_ms: int,
    spread_fraction: float,
) -> dict[str, Any]:
    """Build a signal packet from an early W3 breakout; still analysis-only."""
    setup = detect_early_wave3_breakout(frame_5m, frame_15m, asof=asof, entry_price=entry_price)
    if setup.get("signal") not in {"LONG", "SHORT"}:
        return {"signal": "NO_TRADE", "reason": setup.get("reason", "No early W3 setup")}
    side = setup["signal"]
    symbol = str(instrument_id).upper()
    if funding_kind != "0" or not math.isfinite(float(funding_rate)):
        return {"signal": "NO_TRADE", "reason": "Regular funding proof is required"}
    if not math.isfinite(float(spread_fraction)) or not 0 <= float(spread_fraction) <= 0.0004:
        return {"signal": "NO_TRADE", "reason": "Spread guard failed"}
    age = int(asof.timestamp() * 1000) - int(funding_event_time_ms)
    if age < 0 or age > int(funding_max_age_ms):
        return {"signal": "NO_TRADE", "reason": "Funding event is stale or future-dated"}
    signal_id = f"challenge1-v2-early-w3:{symbol}:{setup['signal_candle_open_utc']}:{side}"
    evidence = {
        "decision_engine_version": "CHALLENGE_1_V2_EARLY_W3_5M_15M",
        "signal_id": signal_id, "instrument_id": symbol,
        "setup_timeframe": "5m", "entry_execution_timeframe": "15m",
        "context_timeframes": ["5m", "15m"], "higher_timeframes_used": False,
        "completed_candles_only": True, "signal_candle_open_utc": setup["signal_candle_open_utc"],
        "context_15m_candle_open_utc": setup["context_15m_candle_open_utc"],
        "data_asof_utc": setup["asof_utc"], "elliott_wave": setup["wave_5m"],
        "early_w3": setup, "funding_kind": "0", "funding_rate": float(funding_rate),
        "funding_event_time_ms": int(funding_event_time_ms),
        "funding_max_age_ms": int(funding_max_age_ms),
    }
    risk = abs(setup["entry"] - setup["stop_loss"])
    return {
        "signal": side, "signal_id": signal_id, "instrument_id": symbol,
        "execution_profile": "CHALLENGE_1_V2", "entry": setup["entry"],
        "stop_loss": setup["stop_loss"], "take_profit": setup["take_profit"],
        "gross_rr": round(abs(setup["take_profit"] - setup["entry"]) / risk, 6),
        "confidence_score": 100.0,
        "score_parts": {"early_w3_breakout": 45, "w2_structural_stop": 25,
                        "fib_1_618_or_1_5r": 20, "completed_15m_alignment": 10},
        "regime": {"status": "FAST_TREND", "direction": side},
        "atr_15m": setup["atr_5m"], "elliott_wave_5m": setup["wave_5m"],
        "challenge1_v2_evidence": evidence, "funding_rate": float(funding_rate),
        "funding_kind": "0", "funding_event_time_ms": int(funding_event_time_ms),
        "funding_max_age_ms": int(funding_max_age_ms), "spread_fraction": float(spread_fraction),
        "signal_candle_open_utc": setup["signal_candle_open_utc"],
        "orders_submitted": False, "fast_trigger_active": True, "early_w3_active": True,
    }





def _utc_timestamp(value: Any) -> pd.Timestamp | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        stamp = pd.Timestamp(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if stamp.tzinfo is None:
        return None
    return stamp.tz_convert("UTC")



def build_fast_trigger_signal(
    *, instrument_id: str, frame_1h: pd.DataFrame, frame_4h: pd.DataFrame,
    frame_15m: pd.DataFrame, btc_15m: pd.DataFrame, asof: pd.Timestamp,
    funding_rate: float | None, funding_kind: str | int | None,
    funding_event_time_ms: int | None, funding_max_age_ms: int | None,
    spread_fraction: float | None, fast_trigger_record: Mapping[str, Any],
) -> dict[str, Any]:
    """Promote the dashboard's confirmed 5m Elliott/candle trigger through V2 risk gates."""
    side = str(fast_trigger_record.get("signal") or "").upper()
    symbol = str(instrument_id or "").upper()
    if side not in {"LONG", "SHORT"} or fast_trigger_record.get("instrument_id") != symbol:
        return {"signal": "NO_TRADE", "reason": "Fast trigger identity or direction is invalid"}
    fast_ready, fast_reason = challenge1_entry_evidence_ready(fast_trigger_record)
    if not fast_ready:
        return {"signal": "NO_TRADE", "reason": "Fast candle evidence rejected: " + fast_reason}
    if asof.tzinfo is None:
        raise ValueError("asof must be timezone-aware")
    asof = asof.tz_convert("UTC")
    m15 = _closed(_validate(frame_15m, "15M", 70), asof, pd.Timedelta(minutes=15))
    if len(m15) < 60:
        return {"signal": "NO_TRADE", "reason": "Fast trigger 15m alignment history is insufficient"}
    e21 = ema(m15["close"], 21)
    if len(e21) < 4 or pd.isna(e21.iloc[-1]) or pd.isna(e21.iloc[-4]):
        return {"signal": "NO_TRADE", "reason": "Fast trigger 15m EMA21 alignment is unavailable"}
    m15_close = float(m15["close"].iloc[-1])
    ema21_value = float(e21.iloc[-1])
    ema21_slope = float(e21.iloc[-1] - e21.iloc[-4])
    aligned = (
        m15_close > ema21_value and ema21_slope > 0
        if side == "LONG" else
        m15_close < ema21_value and ema21_slope < 0
    )
    if not aligned:
        return {"signal": "NO_TRADE", "reason": "Confirmed 5m trigger conflicts with completed 15m EMA21 trend"}
    if funding_rate is None or spread_fraction is None or str(funding_kind or "").strip() != "0":
        return {"signal": "NO_TRADE", "reason": "Fast trigger funding classification or spread evidence is missing"}
    if (
        isinstance(funding_event_time_ms, bool) or not isinstance(funding_event_time_ms, int)
        or isinstance(funding_max_age_ms, bool) or not isinstance(funding_max_age_ms, int)
        or funding_event_time_ms <= 0 or funding_max_age_ms <= 0
    ):
        return {"signal": "NO_TRADE", "reason": "Fast trigger funding freshness evidence is invalid"}
    asof_ms = int(asof.timestamp() * 1000)
    funding_age = asof_ms - funding_event_time_ms
    if funding_age < 0 or funding_age > funding_max_age_ms:
        return {"signal": "NO_TRADE", "reason": "Fast trigger funding event is stale or future-dated"}
    funding = float(funding_rate)
    spread = float(spread_fraction)
    if not math.isfinite(funding) or not math.isfinite(spread) or spread < 0 or spread > 0.0004:
        return {"signal": "NO_TRADE", "reason": "Fast trigger funding or spread guard failed"}
    if (side == "LONG" and funding > 0.0005) or (side == "SHORT" and funding < -0.0005):
        return {"signal": "NO_TRADE", "reason": "Fast trigger funding crowding guard blocked entry"}

    btc = _closed(_validate(btc_15m, "BTC 15M", 40), asof, pd.Timedelta(minutes=15))
    if len(btc) < 25:
        return {"signal": "NO_TRADE", "reason": "Fast trigger BTC master feed is unavailable"}
    btc_ema21 = ema(btc["close"], 21)
    btc_change = float(btc["close"].iloc[-1] / btc["close"].iloc[-2] - 1.0)
    btc_ok = True if symbol == "BTC-USDT" else (
        float(btc["close"].iloc[-1]) > float(btc_ema21.iloc[-1]) and btc_change > -0.008
        if side == "LONG" else
        float(btc["close"].iloc[-1]) < float(btc_ema21.iloc[-1]) and btc_change < 0.008
    )
    if not btc_ok:
        return {"signal": "NO_TRADE", "reason": "Fast trigger BTC master guard blocked the setup"}

    entry = float(fast_trigger_record.get("entry_price_usd") or 0)
    stop = float(fast_trigger_record.get("stop_loss_usd") or 0)
    if not all(math.isfinite(x) and x > 0 for x in (entry, stop)):
        return {"signal": "NO_TRADE", "reason": "Fast trigger entry or structural stop is invalid"}
    risk = entry - stop if side == "LONG" else stop - entry
    stop_pct = risk / entry if entry > 0 else math.inf
    if risk <= 0 or stop_pct > MAX_STOP_DISTANCE_PCT:
        return {"signal": "NO_TRADE", "reason": "Fast trigger structural stop exceeds the 3.5% risk cap"}
    target = entry + 1.5 * risk if side == "LONG" else entry - 1.5 * risk
    if target <= 0:
        return {"signal": "NO_TRADE", "reason": "Fast trigger TP1 calculation is invalid"}

    h1 = _closed(_validate(frame_1h, "1H", 205), asof, pd.Timedelta(hours=1))
    h4 = _closed(_validate(frame_4h, "4H", 205), asof, pd.Timedelta(hours=4))
    if h1.empty or h4.empty:
        return {"signal": "NO_TRADE", "reason": "Fast trigger higher-timeframe context is unavailable"}
    regime = _regime(frame_1h, frame_4h, asof)
    fast_evidence = fast_trigger_record["challenge1_evidence"]
    wave = dict(fast_evidence["elliott_wave"])
    wave.update({
        "timeframe": "5m",
        "evidence_source": "DASHBOARD_CHALLENGE1_COMPLETED_5M_CANDLES",
        "expected_signal_direction": side,
        "latest_closed_candle_open_utc": fast_trigger_record["signal_bar_open_utc"],
        "data_asof_utc": asof.isoformat(),
        "wave_alignment": "ALIGNED",
    })
    signal_time = str(fast_trigger_record["signal_bar_open_utc"])
    signal_id = f"challenge1-v2-fast:{symbol}:5m:{signal_time}:{side}"
    score_parts = {
        "confirmed_5m_pattern": 35,
        "confirmed_5m_elliott_wave": 35,
        "completed_15m_ema21_alignment": 20,
        "btc_master_alignment": 10 if btc_ok else 0,
    }
    confidence = sum(score_parts.values())
    evidence = {
        "decision_engine_version": "CHALLENGE_1_V2_FAST_5M_15M_ELLIOTT",
        "signal_id": signal_id,
        "instrument_id": symbol,
        "setup_timeframe": "5m",
        "entry_execution_timeframe": "15m",
        "context_timeframes": ["15m", "1H", "4H"],
        "higher_timeframes_used": True,
        "completed_candles_only": True,
        "signal_candle_open_utc": signal_time,
        "context_15m_candle_open_utc": m15.index[-1].isoformat(),
        "context_1h_candle_open_utc": h1.index[-1].isoformat(),
        "context_4h_candle_open_utc": h4.index[-1].isoformat(),
        "data_asof_utc": asof.isoformat(),
        "funding_rate": funding,
        "funding_kind": "0",
        "funding_event_time_ms": funding_event_time_ms,
        "funding_max_age_ms": funding_max_age_ms,
        "elliott_wave": wave,
        "wave_alignment": "ALIGNED",
        "fast_trigger": dict(fast_trigger_record),
        "m15_alignment": {
            "timeframe": "15m",
            "evidence_source": "BLOFIN_DEMO_COMPLETED_15M_CANDLES",
            "completed_candles_only": True,
            "latest_closed_candle_open_utc": m15.index[-1].isoformat(),
            "close": m15_close,
            "ema21": ema21_value,
            "ema21_slope_3_bars": ema21_slope,
            "direction": side,
            "aligned": True,
        },
    }
    return {
        "signal": side, "signal_id": signal_id, "instrument_id": symbol,
        "execution_profile": "CHALLENGE_1_V2",
        "entry": entry, "stop_loss": stop, "take_profit": target,
        "atr_15m": float(atr(m15, 14).iloc[-1]),
        "gross_rr": 1.5, "confidence_score": confidence,
        "score_parts": score_parts, "regime": regime,
        "volume_ratio": fast_evidence.get("volume_confirmation", {}).get("volume_ratio"),
        "elliott_wave_5m": wave, "challenge1_v2_evidence": evidence,
        "funding_rate": funding, "funding_kind": "0",
        "funding_event_time_ms": funding_event_time_ms,
        "funding_max_age_ms": funding_max_age_ms,
        "spread_fraction": spread, "signal_candle_open_utc": signal_time,
        "orders_submitted": False,
        "fast_trigger_active": True,
    }


def _fast_trigger_execution_evidence_ready(record: Mapping[str, Any]) -> tuple[bool, str]:
    evidence = record.get("challenge1_v2_evidence")
    if not isinstance(evidence, Mapping):
        return False, "Fast V2 evidence is missing."
    side = str(record.get("signal") or "").upper()
    symbol = str(record.get("instrument_id") or "").upper()
    signal_id = str(record.get("signal_id") or "")
    signal_open = _utc_timestamp(evidence.get("signal_candle_open_utc"))
    asof = _utc_timestamp(evidence.get("data_asof_utc"))
    h1_open = _utc_timestamp(evidence.get("context_1h_candle_open_utc"))
    h4_open = _utc_timestamp(evidence.get("context_4h_candle_open_utc"))
    m15_open = _utc_timestamp(evidence.get("context_15m_candle_open_utc"))
    fast = evidence.get("fast_trigger")
    m15 = evidence.get("m15_alignment")
    if (
        evidence.get("decision_engine_version") != "CHALLENGE_1_V2_FAST_5M_15M_ELLIOTT"
        or evidence.get("signal_id") != signal_id
        or evidence.get("instrument_id") != symbol
        or evidence.get("setup_timeframe") != "5m"
        or evidence.get("entry_execution_timeframe") != "15m"
        or evidence.get("context_timeframes") != ["15m", "1H", "4H"]
        or evidence.get("completed_candles_only") is not True
        or evidence.get("higher_timeframes_used") is not True
        or evidence.get("signal_candle_open_utc") != record.get("signal_candle_open_utc")
        or any(x is None for x in (signal_open, asof, h1_open, h4_open, m15_open))
        or signal_id != f"challenge1-v2-fast:{symbol}:5m:{signal_open.isoformat()}:{side}"
        or not isinstance(fast, Mapping)
        or not isinstance(m15, Mapping)
    ):
        return False, "Fast V2 identity, timeframe or timestamp evidence is invalid."
    raw_ready, raw_reason = challenge1_entry_evidence_ready(fast)
    if not raw_ready or fast.get("signal") != side or fast.get("instrument_id") != symbol:
        return False, "Dashboard fast-trigger evidence failed: " + raw_reason
    if (
        m15.get("timeframe") != "15m"
        or m15.get("evidence_source") != "BLOFIN_DEMO_COMPLETED_15M_CANDLES"
        or m15.get("completed_candles_only") is not True
        or m15.get("aligned") is not True
        or str(m15.get("direction") or "").upper() != side
        or _utc_timestamp(m15.get("latest_closed_candle_open_utc")) != m15_open
        or float(m15.get("close") or 0) <= 0
        or float(m15.get("ema21") or 0) <= 0
        or (float(m15.get("close") or 0) <= float(m15.get("ema21") or 0) if side == "LONG" else float(m15.get("close") or 0) >= float(m15.get("ema21") or 0))
        or (float(m15.get("ema21_slope_3_bars") or 0) <= 0 if side == "LONG" else float(m15.get("ema21_slope_3_bars") or 0) >= 0)
    ):
        return False, "Completed 15m trend alignment evidence is invalid."
    for open_time, duration, label in (
        (signal_open, pd.Timedelta(minutes=5), "5M"),
        (m15_open, pd.Timedelta(minutes=15), "15M"),
        (h1_open, pd.Timedelta(hours=1), "1H"),
        (h4_open, pd.Timedelta(hours=4), "4H"),
    ):
        age = asof - (open_time + duration)
        if age < pd.Timedelta(0) or age > duration:
            return False, f"Fast {label} candle is future-dated or stale."
    try:
        entry, stop, target = (float(record.get(k)) for k in ("entry", "stop_loss", "take_profit"))
        score = float(record.get("confidence_score"))
        rr = float(record.get("gross_rr"))
        funding = float(record.get("funding_rate"))
        spread = float(record.get("spread_fraction"))
        parts = record.get("score_parts")
        total = sum(float(v) for v in parts.values())
        funding_time = int(record.get("funding_event_time_ms"))
        funding_max_age = int(record.get("funding_max_age_ms"))
    except (TypeError, ValueError, OverflowError, AttributeError):
        return False, "Fast V2 trade-plan or funding fields are invalid."
    if (
        not all(math.isfinite(x) for x in (entry, stop, target, score, rr, funding, spread, total))
        or entry <= 0 or stop <= 0 or target <= 0 or score < 70
        or abs(rr - 1.5) > 1e-6 or abs(total - score) > 1e-9
        or abs(entry - stop) / entry > MAX_STOP_DISTANCE_PCT
        or abs((abs(target - entry) / abs(entry - stop)) - 1.5) > 1e-6
        or spread < 0 or spread > 0.0004
        or evidence.get("funding_kind") != "0"
        or record.get("funding_kind") != "0"
        or int(asof.timestamp() * 1000) - funding_time < 0
        or int(asof.timestamp() * 1000) - funding_time > funding_max_age
        or (side == "LONG" and not stop < entry < target)
        or (side == "SHORT" and not target < entry < stop)
    ):
        return False, "Fast V2 risk, funding, score or reward/risk contract failed."
    return True, "Confirmed 5m dashboard trigger aligned with completed 15m trend; Demo risk and AI gates remain mandatory."


def challenge1_v2_execution_evidence_ready(record: Any) -> tuple[bool, str]:
    """Preflight V2 analysis evidence; this function never authorizes an order.

    The Demo executor must still obtain current contract/quote/account facts,
    durable reservation, protective-order readiness and two independent,
    signal-bound AI approvals before any order write.
    """
    if not isinstance(record, Mapping):
        return False, "V2 analysis record is missing."
    side = str(record.get("signal") or "").strip().upper()
    symbol = str(record.get("instrument_id") or "").strip().upper()
    if side not in {"LONG", "SHORT"} or not symbol.endswith("-USDT"):
        return False, "V2 signal direction or USDT instrument is invalid."
    if record.get("orders_submitted") is not False:
        return False, "V2 analysis packet must remain analysis-only."
    evidence = record.get("challenge1_v2_evidence")
    if not isinstance(evidence, Mapping):
        return False, "V2-specific execution evidence is missing."

    if evidence.get("decision_engine_version") == "CHALLENGE_1_V2_EARLY_W3_5M_15M":
        early = evidence.get("early_w3")
        try:
            entry, stop, target = (float(record.get(k)) for k in ("entry", "stop_loss", "take_profit"))
            score = float(record.get("confidence_score"))
            rr = float(record.get("gross_rr"))
            funding_time = int(record.get("funding_event_time_ms"))
            funding_max_age = int(record.get("funding_max_age_ms"))
            asof_early = _utc_timestamp(evidence.get("data_asof_utc"))
            signal_open_early = _utc_timestamp(evidence.get("signal_candle_open_utc"))
            m15_open_early = _utc_timestamp(evidence.get("context_15m_candle_open_utc"))
        except (TypeError, ValueError, OverflowError):
            return False, "Early W3 trade-plan evidence is invalid."
        if not isinstance(early, Mapping) or any(x is None for x in (asof_early, signal_open_early, m15_open_early)):
            return False, "Early W3 timestamps or structure evidence are missing."
        if (
            evidence.get("signal_id") != record.get("signal_id")
            or evidence.get("instrument_id") != symbol
            or evidence.get("completed_candles_only") is not True
            or evidence.get("higher_timeframes_used") is not False
            or evidence.get("context_timeframes") != ["5m", "15m"]
            or evidence.get("signal_candle_open_utc") != record.get("signal_candle_open_utc")
            or _utc_timestamp(early.get("asof_utc")) != asof_early
            or _utc_timestamp(early.get("signal_candle_open_utc")) != signal_open_early
            or _utc_timestamp(early.get("context_15m_candle_open_utc")) != m15_open_early
            or not all(math.isfinite(x) for x in (entry, stop, target, score, rr))
            or entry <= 0 or stop <= 0 or target <= 0 or score < 70
            or abs(entry-stop)/entry > MAX_STOP_DISTANCE_PCT
            or rr < 1.5 - 1e-6
            or abs(abs(target-entry)/abs(entry-stop)-rr) > 1e-5
            or record.get("orders_submitted") is not False
            or record.get("funding_kind") != "0" or evidence.get("funding_kind") != "0"
            or int(asof_early.timestamp()*1000)-funding_time < 0
            or int(asof_early.timestamp()*1000)-funding_time > funding_max_age
            or (side == "LONG" and not stop < entry < target)
            or (side == "SHORT" and not target < entry < stop)
        ):
            return False, "Early W3 identity, chronology, funding or risk contract failed."
        return True, "Closed-candle early W3 breakout with anti-chase and structural W2 protection."

    if evidence.get("decision_engine_version") == "CHALLENGE_1_V2_FAST_5M_15M_ELLIOTT":
        return _fast_trigger_execution_evidence_ready(record)
    signal_id = str(record.get("signal_id") or "").strip()
    expected_time = str(record.get("signal_candle_open_utc") or "").strip()
    if (
        evidence.get("decision_engine_version") not in {
            "CHALLENGE_1_V2_1H_4H_15M",
            "CHALLENGE_1_V2_1H_ADX_EMA21_15M_EXECUTION",
        }
        or evidence.get("signal_id") != signal_id
        or evidence.get("instrument_id") != symbol
        or evidence.get("setup_timeframe") not in {"15m", "1H"}
        or evidence.get("entry_execution_timeframe", "15m") != "15m"
        or evidence.get("context_timeframes") != ["1H", "4H"]
        or evidence.get("higher_timeframes_used") is not True
        or evidence.get("completed_candles_only") is not True
        or evidence.get("signal_candle_open_utc") != expected_time
    ):
        return False, "V2 timeframe, identity or closed-candle evidence contract failed."

    asof = _utc_timestamp(evidence.get("data_asof_utc"))
    signal_open = _utc_timestamp(expected_time)
    h1_open = _utc_timestamp(evidence.get("context_1h_candle_open_utc"))
    h4_open = _utc_timestamp(evidence.get("context_4h_candle_open_utc"))
    if any(value is None for value in (asof, signal_open, h1_open, h4_open)):
        return False, "V2 as-of or higher-timeframe candle timestamp is missing or timezone-naive."
    if signal_id != f"challenge1-v2:{symbol}:15m:{signal_open.isoformat()}:{side}":
        return False, "V2 signal_id does not match instrument, setup candle and direction."
    for open_time, duration, label in (
        (signal_open, pd.Timedelta(minutes=15), "15M"),
        (h1_open, pd.Timedelta(hours=1), "1H"),
        (h4_open, pd.Timedelta(hours=4), "4H"),
    ):
        age = asof - (open_time + duration)
        if age < pd.Timedelta(0) or age > duration:
            return False, f"{label} closed candle is future-dated or stale."
    regime = record.get("regime")
    if (
        not isinstance(regime, Mapping)
        or str(regime.get("direction") or "").upper() != side
        or str(regime.get("status") or "").upper() != "TREND"
                or regime.get("candle_open_utc") != evidence.get("context_1h_candle_open_utc")
        or regime.get("candle_open_utc_4h") != evidence.get("context_4h_candle_open_utc")
    ):
        return False, "V2 1H regime or timeframe identity evidence is invalid."

    wave = evidence.get("elliott_wave")
    if not isinstance(wave, Mapping):
        return False, "V2 higher-timeframe Elliott evidence is missing."
    expected_pattern = "BULLISH" if side == "LONG" else "BEARISH"
    allowed_phases = {
        "WAVES_1_AND_2_CONFIRMED",
        "WAVE_3_CONFIRMED",
        "WAVE_4_CONFIRMED",
        "FIVE_WAVE_IMPULSE_CONFIRMED",
    }
    wave_status = str(wave.get("status") or "").upper()
    phase = str(wave.get("phase") or "").upper()
    wave_pattern = str(wave.get("pattern") or "").upper()
    wave_alignment = str(wave.get("wave_alignment") or "").upper()
    wave_unconfirmed = (
        wave_status in {"AMBIGUOUS", "UNKNOWN", "INSUFFICIENT_DATA"}
        or phase in {"AMBIGUOUS", "INSUFFICIENT_DATA", "UNKNOWN"}
        or wave_pattern in {"AMBIGUOUS", "UNKNOWN", "NONE"}
        or wave_alignment in {"UNCONFIRMED", "AMBIGUOUS"}
    )
    if (
        wave.get("timeframe") != "1H"
        or wave.get("evidence_source") != "CONFIRMED_CLOSED_1H_CANDLES"
        or _utc_timestamp(wave.get("data_asof_utc")) != asof
        or _utc_timestamp(wave.get("latest_closed_candle_open_utc")) != h1_open
        or wave_alignment == "OPPOSED"
        or (not wave_unconfirmed and (
            wave_status not in {"CANDIDATE", "CONFIRMED"}
            or wave_pattern != expected_pattern
            or phase not in allowed_phases
            or wave_alignment != "ALIGNED"
        ))
    ):
        return False, "V2 Elliott evidence is directly opposed or violates its as-of contract."
    if not wave_unconfirmed:
        try:
            pivot_count = int(wave.get("confirmed_pivot_count"))
            invalidation = float(wave.get("invalidation_price"))
        except (TypeError, ValueError, OverflowError):
            return False, "V2 Elliott pivot evidence is invalid."
        if isinstance(wave.get("confirmed_pivot_count"), bool) or pivot_count < 3:
            return False, "V2 Elliott history has fewer than three confirmed pivots."
        if not math.isfinite(invalidation) or invalidation <= 0:
            return False, "V2 Elliott invalidation price is missing or invalid."
        pivots = wave.get("pivots")
        if not isinstance(pivots, list) or len(pivots) < 3:
            return False, "V2 Elliott pivot evidence is incomplete."
        previous_pivot = previous_confirmation = None
        for pivot in pivots:
            if not isinstance(pivot, Mapping):
                return False, "V2 Elliott pivot row is malformed."
            pivot_time = _utc_timestamp(pivot.get("timestamp"))
            confirmed_at = _utc_timestamp(pivot.get("confirmed_at"))
            if (
                pivot_time is None or confirmed_at is None
                or pivot_time > confirmed_at or confirmed_at > asof
                or (previous_pivot is not None and pivot_time <= previous_pivot)
                or (previous_confirmation is not None and confirmed_at < previous_confirmation)
            ):
                return False, "V2 Elliott pivot chronology violates the as-of boundary."
            previous_pivot, previous_confirmation = pivot_time, confirmed_at

    if (
        record.get("funding_kind") != "0"
        or evidence.get("funding_kind") != "0"
        or evidence.get("funding_rate") != record.get("funding_rate")
        or evidence.get("funding_event_time_ms") != record.get("funding_event_time_ms")
        or evidence.get("funding_max_age_ms") != record.get("funding_max_age_ms")
    ):
        return False, "V2 funding type/rate proof is missing or inconsistent."
    funding_time_ms = record.get("funding_event_time_ms")
    funding_max_age_ms = record.get("funding_max_age_ms")
    if (
        isinstance(funding_time_ms, bool) or not isinstance(funding_time_ms, int)
        or funding_time_ms <= 0
        or isinstance(funding_max_age_ms, bool) or not isinstance(funding_max_age_ms, int)
        or funding_max_age_ms <= 0
    ):
        return False, "V2 funding event timestamp or freshness contract is invalid."
    funding_age_ms = int(asof.timestamp() * 1000) - funding_time_ms
    if funding_age_ms < 0 or funding_age_ms > funding_max_age_ms:
        return False, "V2 funding event is future-dated or stale."

    try:
        entry = float(record.get("entry"))
        stop = float(record.get("stop_loss"))
        target = float(record.get("take_profit"))
        score = float(record.get("confidence_score"))
        gross_rr = float(record.get("gross_rr"))
        funding = float(record.get("funding_rate"))
        spread = float(record.get("spread_fraction"))
    except (TypeError, ValueError, OverflowError):
        return False, "V2 entry, stop, target, score, Funding or spread data is missing."
    numeric = (entry, stop, target, score, gross_rr, funding, spread)
    if not all(math.isfinite(value) for value in numeric):
        return False, "V2 trade plan contains non-finite values."
    if entry <= 0 or stop <= 0 or target <= 0 or score < 70 or gross_rr < 1.8:
        return False, "V2 trade plan fails its positive-price, score or gross reward/risk floor."
    if spread < 0 or spread > 0.0004:
        return False, "V2 spread is outside its supported range."
    if side == "LONG" and not stop < entry < target:
        return False, "V2 LONG stop/entry/target ordering is invalid."
    if side == "SHORT" and not target < entry < stop:
        return False, "V2 SHORT stop/entry/target ordering is invalid."
    parts = record.get("score_parts")
    if not isinstance(parts, Mapping):
        return False, "V2 confidence score components are missing."
    try:
        component_total = sum(float(value) for value in parts.values())
    except (TypeError, ValueError, OverflowError):
        return False, "V2 confidence score components are invalid."
    if not math.isfinite(component_total) or abs(component_total - score) > 1e-9:
        return False, "V2 confidence score does not reconcile to its components."
    return True, (
        "V2 analysis evidence is structurally valid; independent AI consensus, live quote, "
        "account readiness and protective-order gates remain mandatory."
    )


def _resolve_stop_atr_multiplier(symbol: str, override: float | None = None) -> float:
    """Resolve the symbol default or an explicit research-only stop ATR multiplier."""
    key = str(symbol or "").strip().upper()
    default = {"BTC-USDT": 1.3, "ETH-USDT": 1.625, "SOL-USDT": 2.25}.get(key, 1.8)
    if override is None:
        return default
    value = float(override)
    if not math.isfinite(value) or not 0.5 <= value <= 4.0:
        raise ValueError("stop ATR multiplier override must be between 0.5 and 4.0")
    return value


def analyze_signal(
    *, instrument_id: str, frame_1h: pd.DataFrame, frame_4h: pd.DataFrame,
    frame_15m: pd.DataFrame, btc_15m: pd.DataFrame, asof: pd.Timestamp,
    funding_rate: float | None, funding_kind: str | int | None,
    funding_event_time_ms: int | None, funding_max_age_ms: int | None,
    spread_fraction: float | None,
    last_three_results: tuple[int, int, int] = (-1, -1, -1),
    tp_r: float = 2.4,
    stop_atr_multiplier_override: float | None = None,
    precomputed_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a completed-candle decision plus ML feature vector source values."""
    if asof.tzinfo is None:
        raise ValueError("asof must be timezone-aware")
    asof = asof.tz_convert("UTC")
    symbol = str(instrument_id).upper()
    regime = _regime(
        frame_1h, frame_4h, asof, precomputed_context=precomputed_context
    )
    if regime["direction"] not in {"LONG", "SHORT"}:
        return {"signal": "NO_TRADE", "reason": regime.get("reason"), "regime": regime}
    if funding_rate is None or spread_fraction is None:
        return {"signal": "NO_TRADE", "reason": "Funding or spread estimate is missing; no zero assumption", "regime": regime}
    if str(funding_kind or "").strip() != "0":
        return {"signal": "NO_TRADE", "reason": "Regular fundingKind=0 proof is missing or unsupported", "regime": regime}
    if (
        isinstance(funding_event_time_ms, bool)
        or not isinstance(funding_event_time_ms, int)
        or funding_event_time_ms <= 0
        or isinstance(funding_max_age_ms, bool)
        or not isinstance(funding_max_age_ms, int)
        or funding_max_age_ms <= 0
    ):
        return {"signal": "NO_TRADE", "reason": "Funding event timestamp or freshness contract is missing", "regime": regime}
    asof_ms = int(asof.timestamp() * 1000)
    funding_age_ms = asof_ms - funding_event_time_ms
    if funding_age_ms < 0 or funding_age_ms > funding_max_age_ms:
        return {"signal": "NO_TRADE", "reason": "Funding event is future-dated or stale", "regime": regime}
    funding = float(funding_rate)
    spread = float(spread_fraction)
    if not math.isfinite(funding) or not math.isfinite(spread) or spread < 0 or spread > 0.0004:
        return {"signal": "NO_TRADE", "reason": "Funding/spread guard failed", "regime": regime}
    direction = regime["direction"]
    if (direction == "LONG" and funding > 0.0005) or (direction == "SHORT" and funding < -0.0005):
        return {"signal": "NO_TRADE", "reason": "Funding crowding guard blocked entry", "regime": regime}

    if precomputed_context is None:
        m15 = _closed(_validate(frame_15m, "15M", 70), asof, pd.Timedelta(minutes=15))
        m15_end = len(m15)
        if len(m15) < 60:
            return {"signal": "NO_TRADE", "reason": "15M history insufficient", "regime": regime}
        close = m15["close"]
        e21, e55 = ema(close, 21), ema(close, 55)
        rsi14, atr14 = rsi(close, 14), atr(m15, 14)
        vol_sma = m15["volume"].rolling(20, min_periods=20).mean()
    else:
        m15, m15_end = _closed_prefix(
            precomputed_context["frames"]["15M"], asof, pd.Timedelta(minutes=15)
        )
        if len(m15) < 60:
            return {"signal": "NO_TRADE", "reason": "15M history insufficient", "regime": regime}
        m15_indicators = precomputed_context["indicators"]["15M"]
        e21 = m15_indicators["ema21"].iloc[:m15_end]
        e55 = m15_indicators["ema55"].iloc[:m15_end]
        rsi14 = m15_indicators["rsi14"].iloc[:m15_end]
        atr14 = m15_indicators["atr14"].iloc[:m15_end]
        vol_sma = m15_indicators["volume_sma20"].iloc[:m15_end]
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
    rsi_low, rsi_high = (35, 68) if direction == "LONG" else (32, 65)
    # No 15m candle-pattern, RSI, value-zone, breakout or volume confirmation
    # is required. Keep those readings as diagnostics only; 15m remains the
    # execution-price / ATR protective-stop timeframe.
    if precomputed_context is None:
        atr_series = atr14.dropna()
    else:
        atr_valid = precomputed_context["indicators"]["15M"]["atr14_valid"]
        atr_valid_end = int(atr_valid.index.searchsorted(m15.index[-1], side="right"))
        atr_series = atr_valid.iloc[:atr_valid_end]
    if len(atr_series) < 25:
        return {"signal": "NO_TRADE", "reason": "ATR shock baseline unavailable", "regime": regime}
    prior_mean = float(atr_series.iloc[-21:-1].mean())
    current_atr = float(atr_series.iloc[-1])
    if prior_mean <= 0 or current_atr > 2.5 * prior_mean:
        return {"signal": "NO_TRADE", "reason": "15M ATR shock exceeds 2.5x its 20-bar baseline", "regime": regime}

    if precomputed_context is None:
        btc = _closed(_validate(btc_15m, "BTC 15M", 40), asof, pd.Timedelta(minutes=15))
        if len(btc) < 25:
            return {"signal": "NO_TRADE", "reason": "BTC master feed unavailable", "regime": regime}
        btc_ema21 = ema(btc["close"], 21)
    else:
        btc, btc_end = _closed_prefix(
            precomputed_context["frames"]["BTC_15M"], asof, pd.Timedelta(minutes=15)
        )
        if len(btc) < 25:
            return {"signal": "NO_TRADE", "reason": "BTC master feed unavailable", "regime": regime}
        btc_ema21 = precomputed_context["indicators"]["BTC_15M"]["ema21"].iloc[:btc_end]
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
    atr_mult = _resolve_stop_atr_multiplier(symbol, stop_atr_multiplier_override)
    if direction == "LONG":
        stop = min(float(m15["low"].tail(5).min()), entry - atr_mult * current_atr)
        target = entry + tp_r * (entry - stop)
    else:
        stop = max(float(m15["high"].tail(5).max()), entry + atr_mult * current_atr)
        target = entry - tp_r * (stop - entry)
    stop_pct = abs(entry - stop) / entry
    if stop_pct <= 0 or stop_pct > MAX_STOP_DISTANCE_PCT:
        return {"signal": "NO_TRADE", "reason": "stop distance breaches 3.5% volatility cap", "regime": regime}

    score_parts = {
        "h1_close_vs_ema21": 35,
        "h1_adx_gte_22": 35 if float(regime["adx_1h"]) >= 22 else 0,
        "h4_alignment_bonus": 20 if regime.get("aligned_4h") else 0,
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
    wave_evidence = _elliott_wave_1h_evidence(
        frame_1h, asof, expected_direction=direction,
        precomputed_context=precomputed_context,
    )
    v2_evidence = {
        "decision_engine_version": "CHALLENGE_1_V2_1H_ADX_EMA21_15M_EXECUTION",
        "signal_id": signal_id,
        "instrument_id": symbol,
        "setup_timeframe": "1H",
        "entry_execution_timeframe": "15m",
        "context_timeframes": ["1H", "4H"],
        "higher_timeframes_used": True,
        "completed_candles_only": True,
        "signal_candle_open_utc": signal_time,
        "context_1h_candle_open_utc": regime.get("candle_open_utc"),
        "context_4h_candle_open_utc": regime.get("candle_open_utc_4h"),
        "data_asof_utc": asof.isoformat(),
        "funding_rate": funding,
        "funding_kind": "0",
        "funding_event_time_ms": funding_event_time_ms,
        "funding_max_age_ms": funding_max_age_ms,
        "elliott_wave": wave_evidence,
        "wave_alignment": wave_evidence["wave_alignment"],
    }
    return {
        "signal": direction, "signal_id": signal_id, "instrument_id": symbol,
        "execution_profile": "CHALLENGE_1_V2",
        "entry": entry, "stop_loss": stop, "take_profit": target,
        "atr_15m": current_atr, "gross_rr": abs(target - entry) / abs(entry - stop),
        "confidence_score": confidence_score, "score_parts": score_parts,
        "regime": regime, "volume_ratio": volume_ratio, "rsi_15m": rsi_value,
        "elliott_wave_1h": wave_evidence,
        "challenge1_v2_evidence": v2_evidence,
        "funding_rate": funding, "funding_kind": "0",
        "funding_event_time_ms": funding_event_time_ms,
        "funding_max_age_ms": funding_max_age_ms,
        "spread_fraction": spread,
        "signal_candle_open_utc": signal_time, "features": features,
        "orders_submitted": False,
    }
