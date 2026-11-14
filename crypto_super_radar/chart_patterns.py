"""Non-repainting geometric chart-pattern evidence for confirmed price pivots.

Patterns are descriptive only: they do not affect confirmation gates or signals.
"""
from __future__ import annotations

from typing import Any, Optional

import numpy as np
import pandas as pd

from .elliott_waves import monitor_elliott_waves


def _unknown(reason: str) -> dict[str, Any]:
    return {
        "status": "UNKNOWN",
        "pattern": "UNKNOWN",
        "breakout": "NONE",
        "upper_touches": 0,
        "lower_touches": 0,
        "pivots": [],
        "upper_line": None,
        "lower_line": None,
        "upper_now": None,
        "lower_now": None,
        "reason": reason,
        "confidence_semantics": "Structural fit label, not a probability or trade recommendation.",
    }


def _fit_line(points: list[dict[str, Any]]) -> tuple[float, float, float]:
    x = np.asarray([float(point["index"]) for point in points], dtype=float)
    y = np.asarray([float(point["price"]) for point in points], dtype=float)
    slope, intercept = np.polyfit(x, y, 1)
    predicted = slope * x + intercept
    total = float(np.sum((y - float(np.mean(y))) ** 2))
    r_squared = 1.0 if total <= 1e-12 else 1.0 - float(np.sum((y - predicted) ** 2)) / total
    return float(slope), float(intercept), max(0.0, min(1.0, r_squared))


def _line_payload(slope: float, intercept: float, start: int, end: int) -> dict[str, float | int]:
    return {
        "start_index": int(start),
        "end_index": int(end),
        "start_price": float(slope * start + intercept),
        "end_price": float(slope * end + intercept),
    }


def classify_confirmed_pivots(
    pivots: list[dict[str, Any]], current_close: float, last_index: int
) -> dict[str, Any]:
    """Classify a converging triangle only after three confirmed touches per side."""
    result = _unknown("At least three confirmed highs and lows are required.")
    if not np.isfinite(current_close) or current_close <= 0 or last_index < 1:
        return {**result, "reason": "Current close or chart index is invalid."}
    selected = [p for p in pivots[-12:] if p.get("kind") in {"HIGH", "LOW"}]
    highs = [p for p in selected if p["kind"] == "HIGH"]
    lows = [p for p in selected if p["kind"] == "LOW"]
    if len(highs) < 3 or len(lows) < 3:
        return {**result, "pivots": selected, "reason": "Fewer than three confirmed contacts on either boundary."}
    if any(
        not isinstance(p.get("index"), (int, float))
        or not np.isfinite(p.get("price", np.nan))
        or p["price"] <= 0
        for p in selected
    ):
        return {**result, "reason": "Pivot coordinates contain invalid values."}

    upper_slope, upper_intercept, upper_fit = _fit_line(highs[-3:])
    lower_slope, lower_intercept, lower_fit = _fit_line(lows[-3:])
    start = min(int(highs[-3]["index"]), int(lows[-3]["index"]))
    end = int(last_index)
    if end <= start:
        return {**result, "pivots": selected, "reason": "Confirmed pivot window is too short."}

    upper_start = upper_slope * start + upper_intercept
    lower_start = lower_slope * start + lower_intercept
    upper_now = upper_slope * end + upper_intercept
    lower_now = lower_slope * end + lower_intercept
    initial_width = upper_start - lower_start
    current_width = upper_now - lower_now
    scale = max(float(np.mean([p["price"] for p in selected])), 1e-12)
    if initial_width <= 0 or current_width <= 0:
        return {**result, "pivots": selected, "reason": "Trend boundaries do not enclose a valid price range."}
    if current_width >= initial_width * 0.85:
        return {**result, "pivots": selected, "reason": "Price range has not contracted enough to confirm a triangle."}
    if min(upper_fit, lower_fit) < 0.75:
        return {**result, "pivots": selected, "reason": "Pivot contacts do not fit stable trend boundaries."}

    flat_limit = scale * 0.002 / max(end - start, 1)
    if upper_slope < -flat_limit and lower_slope > flat_limit:
        pattern = "SYMMETRICAL_TRIANGLE"
    elif abs(upper_slope) <= flat_limit and lower_slope > flat_limit:
        pattern = "ASCENDING_TRIANGLE"
    elif upper_slope < -flat_limit and abs(lower_slope) <= flat_limit:
        pattern = "DESCENDING_TRIANGLE"
    else:
        return {**result, "pivots": selected, "reason": "Boundaries are not converging in a recognized triangle structure."}

    if current_close > upper_now * 1.0025:
        status, breakout = "BREAKOUT_UP", "UP"
    elif current_close < lower_now * 0.9975:
        status, breakout = "BREAKOUT_DOWN", "DOWN"
    else:
        status, breakout = "FORMING", "NONE"

    return {
        "status": status,
        "pattern": pattern,
        "breakout": breakout,
        "upper_touches": len(highs[-3:]),
        "lower_touches": len(lows[-3:]),
        "pivots": selected[-6:],
        "upper_line": _line_payload(upper_slope, upper_intercept, start, end),
        "lower_line": _line_payload(lower_slope, lower_intercept, start, end),
        "upper_now": float(upper_now),
        "lower_now": float(lower_now),
        "range_contraction_pct": round((1.0 - current_width / initial_width) * 100.0, 2),
        "fit_quality": round(min(upper_fit, lower_fit), 3),
        "reason": "Three confirmed contacts per boundary; breakout state uses the latest candle close.",
        "confidence_semantics": "Structural fit label, not a probability or trade recommendation.",
    }


def confirm_breakout_with_volume(
    breakout: str, candles: Optional[pd.DataFrame], *, min_volume_ratio: float = 1.2
) -> dict[str, Any]:
    """Confirm a triangle break with relative volume and close location."""
    if breakout not in {"UP", "DOWN"}:
        return {"status": "NOT_APPLICABLE", "volume_ratio": None, "close_location": None}
    required = {"high", "low", "close", "volume"}
    if not isinstance(candles, pd.DataFrame) or len(candles) < 21 or not required.issubset(candles.columns):
        return {"status": "UNKNOWN", "volume_ratio": None, "close_location": None,
                "reason": "Twenty prior volume bars and complete OHLC data are required."}
    recent = candles.loc[:, ["high", "low", "close", "volume"]].tail(21).apply(
        pd.to_numeric, errors="coerce"
    )
    if recent.isna().any().any() or not np.isfinite(recent.to_numpy(dtype=float)).all():
        return {"status": "UNKNOWN", "volume_ratio": None, "close_location": None,
                "reason": "Recent OHLCV observations are missing or non-finite."}
    if (recent[["high", "low", "close"]] <= 0).any().any() or (recent["volume"] < 0).any():
        return {"status": "UNKNOWN", "volume_ratio": None, "close_location": None,
                "reason": "Recent OHLCV observations contain invalid values."}
    baseline = float(recent["volume"].iloc[:-1].median())
    bar_range = float(recent["high"].iloc[-1] - recent["low"].iloc[-1])
    if baseline <= 0 or bar_range <= 0:
        return {"status": "UNKNOWN", "volume_ratio": None, "close_location": None,
                "reason": "A positive volume baseline and candle range are required."}
    ratio = float(recent["volume"].iloc[-1] / baseline)
    location = float((recent["close"].iloc[-1] - recent["low"].iloc[-1]) / bar_range)
    close_confirmed = location >= 0.65 if breakout == "UP" else location <= 0.35
    confirmed = ratio >= min_volume_ratio and close_confirmed
    return {
        "status": "CONFIRMED" if confirmed else "UNCONFIRMED",
        "volume_ratio": round(ratio, 3),
        "close_location": round(location, 3),
        "reason": "Requires at least 1.2x median volume and a close near the breakout-side candle extreme.",
    }


def contradicts_confirmed_breakout(direction: Optional[str], pattern: dict[str, Any]) -> bool:
    """Return True only when a confirmed triangle breakout opposes the market direction."""
    direction_value = str(direction or "").strip().upper()
    breakout = str((pattern or {}).get("breakout", "NONE")).strip().upper()
    confirmed = (pattern or {}).get("breakout_confirmation") == "CONFIRMED"
    return confirmed and (
        (direction_value == "LONG" and breakout == "DOWN")
        or (direction_value == "SHORT" and breakout == "UP")
    )


def detect_hammer_candlestick(candles: Optional[pd.DataFrame]) -> dict[str, Any]:
    """Detect a hammer after a downtrend; setup only, never a standalone trade signal.

    Uses the latest supplied completed candle. A later close above its high is
    required for confirmation and is reported separately when available.
    """
    unknown = {
        "status": "UNKNOWN", "pattern": "NONE", "direction": "NONE",
        "body": None, "lower_shadow": None, "upper_shadow": None,
        "lower_shadow_body_ratio": None, "confirmation": "PENDING",
        "reason": "At least six valid timestamped OHLC candles are required.",
    }
    required = {"open", "high", "low", "close"}
    if not isinstance(candles, pd.DataFrame) or not required.issubset(candles.columns):
        return unknown
    if not isinstance(candles.index, pd.DatetimeIndex) or len(candles) < 6:
        return unknown
    frame = candles.loc[:, ["open", "high", "low", "close"]].tail(7).apply(
        pd.to_numeric, errors="coerce"
    )
    values = frame.to_numpy(dtype=float)
    if not np.isfinite(values).all() or (frame <= 0).any().any():
        return {**unknown, "reason": "OHLC data contains missing or invalid values."}
    if ((frame["high"] < frame[["open", "close", "low"]].max(axis=1)).any()
        or (frame["low"] > frame[["open", "close", "high"]].min(axis=1)).any()):
        return {**unknown, "reason": "OHLC candle boundaries are inconsistent."}

    candle = frame.iloc[-1]
    body = abs(float(candle["close"] - candle["open"]))
    lower = float(min(candle["open"], candle["close"]) - candle["low"])
    upper = float(candle["high"] - max(candle["open"], candle["close"]))
    candle_range = float(candle["high"] - candle["low"])
    if candle_range <= 0:
        return {**unknown, "reason": "Latest candle has no valid trading range."}
    # A tiny body is handled with a range-relative floor to avoid division by zero.
    body_floor = max(body, candle_range * 0.01)
    ratio = lower / body_floor
    small_body = body <= candle_range * 0.35
    long_lower = lower >= 2.0 * body_floor
    tiny_upper = upper <= max(body_floor * 0.25, candle_range * 0.05)
    prior = frame["close"].iloc[:-1]
    downtrend = bool(
        len(prior) >= 5
        and float(prior.iloc[0]) > float(prior.iloc[-1])
        and float(prior.iloc[-1]) <= float(prior.max()) * 0.99
    )
    is_hammer = bool(downtrend and small_body and long_lower and tiny_upper)
    confirmation = "PENDING"
    if is_hammer and len(frame) >= 2:
        # The next candle confirms only if it closes above the hammer high.
        # For the latest candle there is no next candle yet.
        confirmation = "PENDING"
    return {
        "status": "SETUP" if is_hammer else "NOT_DETECTED",
        "pattern": "HAMMER" if is_hammer else "NONE",
        "direction": "BULLISH" if is_hammer else "NONE",
        "body": round(body, 10),
        "lower_shadow": round(lower, 10),
        "upper_shadow": round(upper, 10),
        "lower_shadow_body_ratio": round(ratio, 3),
        "small_body": small_body,
        "long_lower_shadow": long_lower,
        "small_upper_shadow": tiny_upper,
        "preceding_downtrend": downtrend,
        "confirmation": confirmation,
        "reason": (
            "Hammer geometry detected after a preceding downtrend; wait for a later candle to close above the hammer high."
            if is_hammer else
            "No valid hammer: downtrend, small body, lower-shadow ratio, and small upper shadow must all qualify."
        ),
        "hammer_high": float(candle["high"]) if is_hammer else None,
        "hammer_low": float(candle["low"]) if is_hammer else None,
        "confidence_semantics": "Pattern setup only; not a probability or trade recommendation.",
    }


def monitor_chart_patterns(candles: Optional[pd.DataFrame]) -> dict[str, Any]:
    """Detect triangle structures from close-only, confirmed non-repainting pivots."""
    if not isinstance(candles, pd.DataFrame) or "close" not in candles:
        return _unknown("Validated candle history is unavailable.")
    if not isinstance(candles.index, pd.DatetimeIndex) or len(candles) < 30:
        return _unknown("At least 30 timestamped candles are required.")
    close = pd.to_numeric(candles["close"], errors="coerce")
    values = close.to_numpy(dtype=float)
    if close.isna().any() or not np.isfinite(values).all() or (values <= 0).any():
        return _unknown("Candle closes contain missing, non-finite, or non-positive values.")
    hammer = detect_hammer_candlestick(candles)
    wave = monitor_elliott_waves(candles)
    result = classify_confirmed_pivots(
        wave.get("pivots", []), float(values[-1]), len(candles) - 1
    )
    confirmation = confirm_breakout_with_volume(result.get("breakout", "NONE"), candles)
    result["breakout_confirmation"] = confirmation["status"]
    result["breakout_volume_ratio"] = confirmation.get("volume_ratio")
    result["breakout_close_location"] = confirmation.get("close_location")
    result["breakout_confirmation_reason"] = confirmation.get("reason")
    result["candlestick_pattern"] = hammer
    return result
