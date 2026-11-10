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
        "pattern": "AMBIGUOUS",
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
    wave = monitor_elliott_waves(candles)
    return classify_confirmed_pivots(
        wave.get("pivots", []), float(values[-1]), len(candles) - 1
    )
