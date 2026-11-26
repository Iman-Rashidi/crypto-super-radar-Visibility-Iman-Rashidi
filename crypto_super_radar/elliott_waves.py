"""Rule-based Elliott impulse-wave monitor with confirmed, non-repainting pivots.

The output is structural evidence, not a forecast probability. Callers may
supply an interval-appropriate reversal floor; the generic default remains
conservative for slower research candles.
"""
from __future__ import annotations

from typing import Any, Optional

import numpy as np
import pandas as pd


def monitor_elliott_waves(
    candles: Optional[pd.DataFrame],
    *,
    min_reversal_pct: float = 0.03,
    max_reversal_pct: float = 0.15,
) -> dict[str, Any]:
    """Describe the latest rule-valid 1-2-3-4-5 impulse count from daily closes.

    A pivot is emitted only after a reversal of the volatility-adjusted threshold.
    Its price timestamp and later confirmation timestamp are both returned, so
    consumers can distinguish where the pivot occurred from when it became known.
    """
    unknown = {
        "status": "UNKNOWN",
        "pattern": "AMBIGUOUS",
        "phase": "INSUFFICIENT_DATA",
        "confirmed_pivot_count": 0,
        "pivots": [],
        "reason": "Validated candle history is unavailable.",
        "confidence_semantics": "Wave labels are structural hypotheses, not probabilities.",
    }
    if not isinstance(candles, pd.DataFrame) or "close" not in candles:
        return unknown
    if not isinstance(candles.index, pd.DatetimeIndex) or len(candles) < 30:
        return {**unknown, "reason": "At least 30 timestamped candles are required."}
    close = pd.to_numeric(candles["close"], errors="coerce")
    if close.isna().any() or not np.isfinite(close.to_numpy(dtype=float)).all() or (close <= 0).any():
        return {**unknown, "reason": "Candle closes contain missing, non-finite, or non-positive values."}

    returns = close.pct_change().abs()
    volatility = float(returns.tail(14).median()) if len(returns) >= 15 else float(returns.median())
    if not np.isfinite(volatility):
        return {**unknown, "reason": "Volatility-based pivot threshold is unavailable."}
    reversal = min(max(float(min_reversal_pct), volatility * 2.0), float(max_reversal_pct))
    if not 0 < reversal < 1:
        return {**unknown, "reason": "Configured pivot threshold is invalid."}

    values = close.to_numpy(dtype=float)
    index = candles.index
    pivots: list[dict[str, Any]] = []
    direction = 0
    high_idx = low_idx = 0
    high = low = values[0]

    def add_pivot(pivot_index: int, kind: str, confirmed_index: int) -> None:
        if pivots and (pivots[-1]["kind"] == kind or pivots[-1]["index"] == pivot_index):
            return
        pivots.append({
            "index": int(pivot_index),
            "kind": kind,
            "price": float(values[pivot_index]),
            "timestamp": index[pivot_index].isoformat(),
            "confirmed_at": index[confirmed_index].isoformat(),
        })

    for i in range(1, len(values)):
        price = values[i]
        if direction == 0:
            high = max(high, price)
            if price >= low * (1.0 + reversal):
                add_pivot(low_idx, "LOW", i)
                direction, high_idx, high = 1, i, price
            else:
                low = min(low, price)
                if price <= high * (1.0 - reversal):
                    add_pivot(high_idx, "HIGH", i)
                    direction, low_idx, low = -1, i, price
        elif direction > 0:
            if price >= high:
                high_idx, high = i, price
            elif price <= high * (1.0 - reversal):
                add_pivot(high_idx, "HIGH", i)
                direction, low_idx, low = -1, i, price
        else:
            if price <= low:
                low_idx, low = i, price
            elif price >= low * (1.0 + reversal):
                add_pivot(low_idx, "LOW", i)
                direction, high_idx, high = 1, i, price

    common = {
        "confirmed_pivot_count": len(pivots),
        "pivot_reversal_threshold_pct": round(reversal * 100.0, 4),
        "pivots": pivots[-6:],
        "confidence_semantics": "Wave labels are structural hypotheses, not probabilities.",
    }
    if len(pivots) < 3:
        return {
            **unknown, **common,
            "reason": "Fewer than three confirmed alternating pivots; wave count is ambiguous.",
        }

    def evaluate(points: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
        prices = [point["price"] for point in points]
        kinds = [point["kind"] for point in points]
        bullish = kinds[0] == "LOW"
        expected = ["LOW" if (i % 2 == 0) == bullish else "HIGH" for i in range(len(points))]
        if kinds != expected:
            return None
        if bullish:
            if not (prices[1] > prices[0] and prices[2] > prices[0] and prices[2] < prices[1]):
                return None
            lengths = [prices[1] - prices[0]]
            if len(points) >= 4:
                if prices[3] <= prices[1]:
                    return None
                lengths.append(prices[3] - prices[2])
            if len(points) >= 5:
                if prices[4] <= prices[1]:
                    return None
            if len(points) >= 6:
                lengths.append(prices[5] - prices[4])
                if prices[5] <= prices[3] or lengths[1] < min(lengths[0], lengths[2]):
                    return None
            invalidation = prices[0] if len(points) == 3 else prices[1]
            return {"direction": "BULLISH", "invalidation_price": float(invalidation)}
        if not (prices[1] < prices[0] and prices[2] < prices[0] and prices[2] > prices[1]):
            return None
        lengths = [prices[0] - prices[1]]
        if len(points) >= 4:
            if prices[3] >= prices[1]:
                return None
            lengths.append(prices[2] - prices[3])
        if len(points) >= 5:
            if prices[4] >= prices[1]:
                return None
        if len(points) >= 6:
            lengths.append(prices[4] - prices[5])
            if prices[5] >= prices[3] or lengths[1] < min(lengths[0], lengths[2]):
                return None
        invalidation = prices[0] if len(points) == 3 else prices[1]
        return {"direction": "BEARISH", "invalidation_price": float(invalidation)}

    selected = None
    selected_points = []
    for size in range(min(6, len(pivots)), 2, -1):
        points = pivots[-size:]
        candidate = evaluate(points)
        if candidate:
            selected = candidate
            selected_points = points
            break

    if selected is None:
        return {
            **common,
            "status": "AMBIGUOUS",
            "pattern": "AMBIGUOUS",
            "phase": "NO_RULE_VALID_IMPULSE",
            "wave_count": None,
            "next_wave": None,
            "invalidation_price": None,
            "reason": "Confirmed pivots do not form a rule-valid impulse prefix.",
        }

    size = len(selected_points)
    if size == 3:
        phase, next_wave, state = "WAVES_1_AND_2_CONFIRMED", 3, "CANDIDATE"
    elif size == 4:
        phase, next_wave, state = "WAVE_3_CONFIRMED", 4, "CANDIDATE"
    elif size == 5:
        phase, next_wave, state = "WAVE_4_CONFIRMED", 5, "CANDIDATE"
    else:
        phase, next_wave, state = "FIVE_WAVE_IMPULSE_CONFIRMED", None, "CONFIRMED"
    return {
        **common,
        "status": state,
        "pattern": selected["direction"],
        "phase": phase,
        "wave_count": size - 1,
        "next_wave": next_wave,
        "invalidation_price": selected["invalidation_price"],
        "reason": (
            "A rule-valid five-wave impulse is complete; corrective direction is not inferred."
            if size == 6 else
            f"A {selected['direction'].lower()} impulse prefix is structurally valid through wave {size - 1}; next wave is only a scenario."
        ),
    }



def detect_impulse_pattern(df: pd.DataFrame, min_ratio_wave3: float = 1.618,
                          max_ratio_wave2: float = 0.786) -> dict[str, Any]:
    """Detect a four-wave impulse from non-repainting pivots confirmed by 2 right bars."""
    empty = {"pattern": "NONE", "phase": "UNCONFIRMED", "confidence": 0.0, "pivots": []}
    if not isinstance(df, pd.DataFrame) or not {"high", "low"}.issubset(df.columns) or len(df) < 9:
        return empty
    highs = pd.to_numeric(df["high"], errors="coerce").to_numpy(dtype=float)
    lows = pd.to_numeric(df["low"], errors="coerce").to_numpy(dtype=float)
    if not np.isfinite(highs).all() or not np.isfinite(lows).all():
        return empty
    pivots = []
    for i in range(2, len(df) - 2):
        if highs[i] > max(highs[i-2:i].max(), highs[i+1:i+3].max()):
            pivots.append({"index": i, "kind": "HIGH", "price": float(highs[i]), "confirmed_at_index": i+2})
        if lows[i] < min(lows[i-2:i].min(), lows[i+1:i+3].min()):
            pivots.append({"index": i, "kind": "LOW", "price": float(lows[i]), "confirmed_at_index": i+2})
    pivots.sort(key=lambda p: p["index"])
    # Collapse adjacent same-kind pivots to the more extreme point.
    compact = []
    for p in pivots:
        if compact and compact[-1]["kind"] == p["kind"]:
            better = p["price"] > compact[-1]["price"] if p["kind"] == "HIGH" else p["price"] < compact[-1]["price"]
            if better: compact[-1] = p
        else: compact.append(p)
    if len(compact) < 5: return {**empty, "pivots": compact[-5:]}
    for pts in (compact[-5:], compact[-5:]):
        kinds = [p["kind"] for p in pts]
        bullish = kinds == ["LOW", "HIGH", "LOW", "HIGH", "LOW"]
        bearish = kinds == ["HIGH", "LOW", "HIGH", "LOW", "HIGH"]
        if not (bullish or bearish): continue
        p0,p1,p2,p3,p4 = [p["price"] for p in pts]
        w1 = abs(p1-p0); w2 = abs(p1-p2); w3 = abs(p3-p2); w4 = abs(p3-p4)
        if min(w1,w2,w3,w4) <= 0: continue
        retr2, ext3, retr4 = w2/w1, w3/w1, w4/w3
        valid = (0.382 <= retr2 <= max_ratio_wave2 and ext3 >= min_ratio_wave3
                 and 0.236 <= retr4 <= 0.50
                 and (p4 > p1 if bullish else p4 < p1))
        if valid:
            fit = min(1.0, (ext3-min_ratio_wave3)/max(min_ratio_wave3, 1e-9) + 0.5)
            confidence = round(70 + 30*fit, 2)
            return {"pattern": "BULLISH" if bullish else "BEARISH", "phase": "WAVE_4_CONFIRMED",
                    "confidence": confidence, "pivots": pts,
                    "ratios": {"wave2_retracement": retr2, "wave3_extension": ext3, "wave4_retracement": retr4}}
    return {**empty, "pivots": compact[-5:]}
