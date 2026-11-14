"""Rule-based multi-family technical pattern analysis for the live market radar.

Pattern detections are descriptive evidence, not probabilities or trade instructions.
Only completed candles are passed by the live radar.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _rsi(close: pd.Series, period: int = 14) -> float | None:
    delta = close.diff()
    gains = delta.clip(lower=0.0).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    losses = (-delta.clip(upper=0.0)).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    if gains.empty or losses.empty or not _finite(gains.iloc[-1]) or not _finite(losses.iloc[-1]):
        return None
    gain, loss = float(gains.iloc[-1]), float(losses.iloc[-1])
    if loss <= 1e-15:
        return 100.0 if gain > 1e-15 else 50.0
    return 100.0 - (100.0 / (1.0 + gain / loss))


def _rsi_series(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gains = delta.clip(lower=0.0).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    losses = (-delta.clip(upper=0.0)).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    ratio = gains / losses.replace(0.0, np.nan)
    result = 100.0 - (100.0 / (1.0 + ratio))
    result = result.where(losses > 0, 100.0)
    return result.where(gains > 0, 50.0)


def _adx(frame: pd.DataFrame, period: int = 14) -> float | None:
    if len(frame) < period * 2 + 2:
        return None
    high = frame["high"].astype(float)
    low = frame["low"].astype(float)
    close = frame["close"].astype(float)
    up = high.diff()
    down = -low.diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    previous = close.shift(1)
    tr = pd.concat([(high - low), (high - previous).abs(), (low - previous).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    plus_di = 100.0 * plus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr.replace(0.0, np.nan)
    minus_di = 100.0 * minus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr.replace(0.0, np.nan)
    denominator = (plus_di + minus_di).replace(0.0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / denominator
    series = dx.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    value = series.iloc[-1]
    return float(value) if _finite(value) else None


def _divergence(frame: pd.DataFrame, pivots: list[dict[str, Any]]) -> dict[str, Any]:
    rsi_values = _rsi_series(frame["close"].astype(float))
    highs = [p for p in pivots if p.get("kind") == "HIGH" and 0 <= int(p.get("index", -1)) < len(frame)]
    lows = [p for p in pivots if p.get("kind") == "LOW" and 0 <= int(p.get("index", -1)) < len(frame)]
    for selected, kind in ((highs, "BEARISH"), (lows, "BULLISH")):
        if len(selected) < 2:
            continue
        first, second = selected[-2:]
        rsi_first, rsi_second = rsi_values.iloc[int(first["index"])], rsi_values.iloc[int(second["index"])]
        if not (_finite(rsi_first) and _finite(rsi_second)):
            continue
        price_condition = (
            float(second["price"]) > float(first["price"]) and float(rsi_second) < float(rsi_first) - 2.0
            if kind == "BEARISH" else
            float(second["price"]) < float(first["price"]) and float(rsi_second) > float(rsi_first) + 2.0
        )
        if price_condition:
            return {
                "type": kind,
                "status": "CANDIDATE",
                "price_pivot_1": float(first["price"]),
                "price_pivot_2": float(second["price"]),
                "price_pivot_1_index": int(first["index"]),
                "price_pivot_2_index": int(second["index"]),
                "price_pivot_1_time": frame.index[int(first["index"])].isoformat(),
                "price_pivot_2_time": frame.index[int(second["index"])].isoformat(),
                "rsi_pivot_1": round(float(rsi_first), 2),
                "rsi_pivot_2": round(float(rsi_second), 2),
                "reason": "Confirmed swing-price and RSI pivots diverge; this is a warning, not a standalone entry.",
            }
    return {"type": "NONE", "status": "NOT_DETECTED", "reason": "No confirmed RSI/price divergence on the available swing pivots."}


def _fibonacci_setup(pivots: list[dict[str, Any]]) -> dict[str, Any]:
    if len(pivots) < 3:
        return {"status": "INSUFFICIENT_PIVOTS", "retracement": None, "entry_zone": None, "invalidation_price": None}
    first, impulse, correction = pivots[-3:]
    if [first.get("kind"), impulse.get("kind"), correction.get("kind")] == ["LOW", "HIGH", "LOW"]:
        start, end, current = map(lambda p: float(p["price"]), (first, impulse, correction))
        length = end - start
        retracement = (end - current) / length if length > 0 else None
        zone = [end - length * 0.618, end - length * 0.5]
        invalidation = start
        direction = "BULLISH"
    elif [first.get("kind"), impulse.get("kind"), correction.get("kind")] == ["HIGH", "LOW", "HIGH"]:
        start, end, current = map(lambda p: float(p["price"]), (first, impulse, correction))
        length = start - end
        retracement = (current - end) / length if length > 0 else None
        zone = [end + length * 0.5, end + length * 0.618]
        invalidation = start
        direction = "BEARISH"
    else:
        return {"status": "NO_ALTERNATING_IMPULSE", "retracement": None, "entry_zone": None, "invalidation_price": None}
    if retracement is None or not math.isfinite(retracement):
        return {"status": "INVALID_STRUCTURE", "retracement": None, "entry_zone": None, "invalidation_price": invalidation}
    return {
        "status": "GOLDEN_ZONE_CANDIDATE" if 0.5 <= retracement <= 0.618 else "OUTSIDE_GOLDEN_ZONE",
        "direction": direction,
        "retracement": round(float(retracement), 4),
        "entry_zone": [round(min(zone), 12), round(max(zone), 12)],
        "invalidation_price": round(invalidation, 12),
        "confirmation_required": True,
    }


def _atr(frame: pd.DataFrame, period: int = 14) -> float | None:
    if len(frame) < period + 1:
        return None
    high = frame["high"].to_numpy(dtype=float)
    low = frame["low"].to_numpy(dtype=float)
    close = frame["close"].to_numpy(dtype=float)
    previous = np.r_[close[0], close[:-1]]
    tr = np.maximum(high - low, np.maximum(np.abs(high - previous), np.abs(low - previous)))
    value = float(np.mean(tr[-period:]))
    return value if _finite(value) and value > 0 else None


def _item(
    *,
    name_fa: str,
    name_en: str,
    category: str,
    direction: str,
    state: str,
    strength: str,
    description: str,
    timestamp: str,
    score: int,
    trigger_price: float | None = None,
    invalidation_price: float | None = None,
    potential_direction: str | None = None,
) -> dict[str, Any]:
    return {
        "name_fa": name_fa,
        "name_en": name_en,
        "category": category,
        "direction": direction,
        "potential_direction": potential_direction,
        "state": state,
        "evidence_strength": strength,
        "description": description,
        "timestamp": timestamp,
        "score": int(score),
        "trigger_price": round(float(trigger_price), 12) if _finite(trigger_price) else None,
        "invalidation_price": round(float(invalidation_price), 12) if _finite(invalidation_price) else None,
    }



def _bearish_abandoned_baby_pattern(frame: pd.DataFrame, volume_ratio: float | None) -> dict[str, Any] | None:
    """Detect strict bearish Abandoned Baby after an uptrend, with two full-range gaps."""
    if len(frame) < 7:
        return None

    candidates = []
    if len(frame) >= 4:
        candidates.append((len(frame) - 4, True))
    candidates.append((len(frame) - 3, False))

    for start, has_follow_through in candidates:
        first, middle, third = (frame.iloc[start + offset] for offset in range(3))
        prior = pd.to_numeric(frame["close"].iloc[max(0, start - 8):start], errors="coerce")
        if len(prior) < 3 or prior.isna().any() or float(prior.iloc[0]) <= 0:
            continue
        prior_change = float(prior.iloc[-1] / prior.iloc[0] - 1.0)
        if prior_change < 0.003:
            continue

        fo, fc = float(first["open"]), float(first["close"])
        fh, fl = float(first["high"]), float(first["low"])
        mo, mc = float(middle["open"]), float(middle["close"])
        mh, ml = float(middle["high"]), float(middle["low"])
        to, tc = float(third["open"]), float(third["close"])
        th, tl = float(third["high"]), float(third["low"])
        first_range, middle_range, third_range = fh - fl, mh - ml, th - tl
        if min(first_range, middle_range, third_range) <= 0:
            continue

        first_body = fc - fo
        middle_body = abs(mc - mo)
        third_body = to - tc
        valid = (
            fc > fo
            and first_body >= first_range * 0.45
            and middle_body <= middle_range * 0.10
            and ml > fh
            and tc < to
            and third_body >= third_range * 0.45
            and th < ml
        )
        if not valid:
            continue

        follow_through = has_follow_through and float(frame.iloc[start + 3]["close"]) < tl
        volume_confirmed = volume_ratio is not None and volume_ratio >= 1.20
        strength = (
            "STRONG" if follow_through and volume_confirmed
            else "MODERATE" if follow_through or volume_confirmed
            else "WEAK"
        )
        description = (
            "پس از روند صعودی، کندل اول صعودی و بلند است؛ دوجی دوم با کل دامنه بالاتر از سقف کندل اول قرار دارد؛ "
            "کندل سوم نزولی و بلند نیز با کل دامنه پایین‌تر از کف دوجی تشکیل شده است. "
            "دو گپِ کامل و بدون هم‌پوشانی، ویژگی کلیدی Bearish Abandoned Baby هستند. "
        )
        if follow_through:
            description += "کندل بعدی پایین‌تر از کف کندل سوم بسته شده و فشار نزولی را تأیید کرده است."
        else:
            description += "برای تأیید بیشتر، بسته‌شدن کندل بعدی پایین‌تر از کف کندل سوم لازم است."
        return _item(
            name_fa="نوزاد رهاشدهٔ نزولی",
            name_en="Bearish Abandoned Baby",
            category="CANDLE",
            direction="BEARISH" if follow_through else "NEUTRAL",
            potential_direction=None if follow_through else "BEARISH",
            state="CONFIRMED" if follow_through else "NEEDS_CONFIRMATION",
            strength=strength,
            description=description,
            timestamp=frame.index[start + 2].isoformat(),
            score=-18 if follow_through else -9,
            trigger_price=tl,
            invalidation_price=fh,
        )
    return None


def _bullish_abandoned_baby_pattern(frame: pd.DataFrame, volume_ratio: float | None) -> dict[str, Any] | None:
    """Detect strict bullish Abandoned Baby with full-range gaps around the Doji."""
    if len(frame) < 7:
        return None

    candidates = []
    if len(frame) >= 4:
        candidates.append((len(frame) - 4, True))
    candidates.append((len(frame) - 3, False))

    for start, has_follow_through in candidates:
        first, middle, third = (frame.iloc[start + offset] for offset in range(3))
        prior = pd.to_numeric(frame["close"].iloc[max(0, start - 8):start], errors="coerce")
        if len(prior) < 3 or prior.isna().any() or float(prior.iloc[0]) <= 0:
            continue
        prior_change = float(prior.iloc[-1] / prior.iloc[0] - 1.0)
        if prior_change > -0.003:
            continue

        fo, fc = float(first["open"]), float(first["close"])
        fh, fl = float(first["high"]), float(first["low"])
        mo, mc = float(middle["open"]), float(middle["close"])
        mh, ml = float(middle["high"]), float(middle["low"])
        to, tc = float(third["open"]), float(third["close"])
        th, tl = float(third["high"]), float(third["low"])
        first_range, middle_range, third_range = fh - fl, mh - ml, th - tl
        if min(first_range, middle_range, third_range) <= 0:
            continue

        first_body = fo - fc
        middle_body = abs(mc - mo)
        third_body = tc - to
        valid = (
            fc < fo
            and first_body >= first_range * 0.45
            and middle_body <= middle_range * 0.10
            and mh < fl
            and tc > to
            and third_body >= third_range * 0.45
            and tl > mh
            and tc >= (fo + fc) / 2.0
        )
        if not valid:
            continue

        follow_through = has_follow_through and float(frame.iloc[start + 3]["close"]) > th
        volume_confirmed = volume_ratio is not None and volume_ratio >= 1.20
        strength = (
            "STRONG" if follow_through and volume_confirmed
            else "MODERATE" if follow_through or volume_confirmed
            else "WEAK"
        )
        description = (
            "پس از روند نزولی، سه کندل تشکیل شده‌اند: کندل اول نزولی و بلند، دوجی دوم با کل دامنه پایین‌تر از کف کندل اول، "
            "و کندل سوم صعودی و بلند با کل دامنه بالاتر از سقف دوجی. این دو گپِ بدون هم‌پوشانی شرط اصلی Abandoned Baby هستند. "
        )
        if follow_through:
            description += "کندل بعدی بالاتر از سقف کندل سوم بسته شده و حرکت صعودی را تأیید کرده است."
        else:
            description += "برای تأیید بیشتر، بسته‌شدن کندل بعدی بالاتر از سقف کندل سوم لازم است."
        return _item(
            name_fa="نوزاد رهاشدهٔ صعودی",
            name_en="Bullish Abandoned Baby",
            category="CANDLE",
            direction="BULLISH" if follow_through else "NEUTRAL",
            potential_direction=None if follow_through else "BULLISH",
            state="CONFIRMED" if follow_through else "NEEDS_CONFIRMATION",
            strength=strength,
            description=description,
            timestamp=frame.index[start + 2].isoformat(),
            score=17 if follow_through else 8,
            trigger_price=th,
            invalidation_price=fl,
        )
    return None

def _rising_window_pattern(frame: pd.DataFrame, volume_ratio: float | None) -> dict[str, Any] | None:
    """Detect a strict bullish Rising Window and track whether its gap remains open."""
    if len(frame) < 6:
        return None

    first_start = max(1, len(frame) - 12)
    for start in range(len(frame) - 2, first_start - 1, -1):
        first, second = frame.iloc[start], frame.iloc[start + 1]
        prior = pd.to_numeric(frame["close"].iloc[max(0, start - 8):start], errors="coerce")
        if len(prior) < 3 or prior.isna().any() or float(prior.iloc[0]) <= 0:
            continue
        prior_change = float(prior.iloc[-1] / prior.iloc[0] - 1.0)
        if prior_change < 0.003:
            continue

        first_high = float(first["high"])
        second_open = float(second["open"])
        second_high = float(second["high"])
        second_low = float(second["low"])
        if not all(_finite(x) for x in (first_high, second_open, second_high, second_low)):
            continue
        if not (second_open > first_high and second_low > first_high and second_high >= second_open):
            continue

        later = frame.iloc[start + 2:]
        gap_filled = not later.empty and bool(
            (pd.to_numeric(later["low"], errors="coerce") <= second_low).any()
        )
        if gap_filled:
            continue

        sustained = not later.empty
        volume_confirmed = volume_ratio is not None and volume_ratio >= 1.20
        strength = (
            "STRONG" if sustained and volume_confirmed
            else "MODERATE" if sustained or volume_confirmed
            else "WEAK"
        )
        gap_size = second_low - first_high
        description = (
            "پس از زمینهٔ صعودی، کندل دوم بالاتر از سقف کندل اول باز شده و کف آن نیز بالاتر از سقف کندل اول مانده است؛ "
            f"بنابراین شکاف صعودیِ بدون هم‌پوشانی به اندازهٔ {gap_size:.8g} تشکیل شده است. "
            f"محدودهٔ شکاف از {first_high:.8g} تا {second_low:.8g} است. "
        )
        if sustained:
            description += "کندل‌های بعدی وارد محدودهٔ شکاف نشده‌اند؛ پنجره همچنان باز و الگو پایدار است."
        else:
            description += "هنوز کندل بعدی برای بررسی بازماندن شکاف وجود ندارد؛ ورود قیمت به محدودهٔ شکاف این الگو را باطل می‌کند."
        return _item(
            name_fa="پنجرهٔ صعودی (گپ رو به بالا)",
            name_en="Rising Window",
            category="CANDLE",
            direction="BULLISH" if sustained else "NEUTRAL",
            potential_direction=None if sustained else "BULLISH",
            state="CONFIRMED" if sustained else "NEEDS_CONFIRMATION",
            strength=strength,
            description=description,
            timestamp=frame.index[start + 1].isoformat(),
            score=15 if sustained else 8,
            trigger_price=second_high,
            invalidation_price=second_low,
        )
    return None


def _morning_star_pattern(frame: pd.DataFrame, volume_ratio: float | None) -> dict[str, Any] | None:
    """Detect a three-candle Morning Star and confirm it only with a later close."""
    if len(frame) < 6:
        return None

    # Prefer a completed follow-through candle; otherwise report the latest forming setup.
    candidates = []
    if len(frame) >= 4:
        candidates.append((len(frame) - 4, True))
    candidates.append((len(frame) - 3, False))

    for start, has_follow_through in candidates:
        first, middle, third = (frame.iloc[start + offset] for offset in range(3))
        prior = pd.to_numeric(frame["close"].iloc[max(0, start - 8):start], errors="coerce")
        if len(prior) < 3 or prior.isna().any() or float(prior.iloc[0]) <= 0:
            continue
        prior_change = float(prior.iloc[-1] / prior.iloc[0] - 1.0)
        if prior_change > -0.003:
            continue

        first_open, first_close = float(first["open"]), float(first["close"])
        first_high, first_low = float(first["high"]), float(first["low"])
        middle_open, middle_close = float(middle["open"]), float(middle["close"])
        middle_high, middle_low = float(middle["high"]), float(middle["low"])
        third_open, third_close = float(third["open"]), float(third["close"])
        third_high, third_low = float(third["high"]), float(third["low"])

        first_range = first_high - first_low
        middle_range = middle_high - middle_low
        third_range = third_high - third_low
        if min(first_range, middle_range, third_range) <= 0:
            continue

        first_body = abs(first_close - first_open)
        middle_body = abs(middle_close - middle_open)
        third_body = abs(third_close - third_open)
        first_midpoint = (first_open + first_close) / 2.0

        valid = (
            first_close < first_open
            and first_body >= first_range * 0.45
            and middle_body <= middle_range * 0.30
            and third_close > third_open
            and third_body >= third_range * 0.45
            and third_close >= first_midpoint
            and third_close > middle_close
        )
        if not valid:
            continue

        follow_through = False
        if has_follow_through:
            confirmation = frame.iloc[start + 3]
            follow_through = float(confirmation["close"]) > third_high

        gap_down = middle_open < first_close
        gap_up = third_open > middle_close
        score = 15 + int(gap_down) + int(gap_up)
        volume_confirmed = volume_ratio is not None and volume_ratio >= 1.20
        state = "CONFIRMED" if follow_through else "NEEDS_CONFIRMATION"
        strength = (
            "STRONG" if follow_through and volume_confirmed
            else "MODERATE" if follow_through or volume_confirmed
            else "WEAK"
        )
        description = (
            "پس از روند نزولی: کندل اول نزولی و بلند، کندل دوم کوچک/دوجی و کندل سوم صعودی و بلند است؛ "
            "بسته‌شدن کندل سوم دست‌کم تا نیمهٔ بدنهٔ اول بازیابی شده است."
        )
        if gap_down or gap_up:
            description += f" گپ نزولی: {'دارد' if gap_down else 'ندارد'}؛ گپ صعودی: {'دارد' if gap_up else 'ندارد'}."
        if follow_through:
            description += " کندل بعدی بالاتر از سقف کندل سوم بسته شده و الگو تأیید شده است."
        else:
            description += " برای تأیید، بسته‌شدن یک کندل بعدی بالاتر از سقف کندل سوم لازم است."

        return _item(
            name_fa="ستاره صبحگاهی",
            name_en="Morning Star",
            category="CANDLE",
            direction="BULLISH" if follow_through else "NEUTRAL",
            potential_direction=None if follow_through else "BULLISH",
            state=state,
            strength=strength,
            description=description,
            timestamp=frame.index[start + 2].isoformat(),
            score=score if follow_through else int(score * 0.5),
            trigger_price=third_high,
            invalidation_price=first_low,
        )
    return None



def _evening_star_pattern(frame: pd.DataFrame, volume_ratio: float | None) -> dict[str, Any] | None:
    """Detect an Evening Star after an uptrend; a later close below its low confirms."""
    if len(frame) < 6:
        return None

    candidates = []
    if len(frame) >= 4:
        candidates.append((len(frame) - 4, True))
    candidates.append((len(frame) - 3, False))

    for start, has_follow_through in candidates:
        first, middle, third = (frame.iloc[start + offset] for offset in range(3))
        prior = pd.to_numeric(frame["close"].iloc[max(0, start - 8):start], errors="coerce")
        if len(prior) < 3 or prior.isna().any() or float(prior.iloc[0]) <= 0:
            continue
        prior_change = float(prior.iloc[-1] / prior.iloc[0] - 1.0)
        if prior_change < 0.003:
            continue

        first_open, first_close = float(first["open"]), float(first["close"])
        first_high, first_low = float(first["high"]), float(first["low"])
        middle_open, middle_close = float(middle["open"]), float(middle["close"])
        middle_high, middle_low = float(middle["high"]), float(middle["low"])
        third_open, third_close = float(third["open"]), float(third["close"])
        third_high, third_low = float(third["high"]), float(third["low"])

        first_range, middle_range, third_range = (
            first_high - first_low, middle_high - middle_low, third_high - third_low
        )
        if min(first_range, middle_range, third_range) <= 0:
            continue

        first_body = abs(first_close - first_open)
        middle_body = abs(middle_close - middle_open)
        third_body = abs(third_close - third_open)
        first_midpoint = (first_open + first_close) / 2.0

        valid = (
            first_close > first_open
            and first_body >= first_range * 0.45
            and middle_body <= middle_range * 0.30
            and third_close < third_open
            and third_body >= third_range * 0.45
            and third_close <= first_midpoint
            and third_close < middle_close
        )
        if not valid:
            continue

        follow_through = False
        if has_follow_through:
            follow_through = float(frame.iloc[start + 3]["close"]) < third_low

        gap_up = middle_open > first_close
        gap_down = third_open < middle_close
        volume_confirmed = volume_ratio is not None and volume_ratio >= 1.20
        strength = (
            "STRONG" if follow_through and volume_confirmed
            else "MODERATE" if follow_through or volume_confirmed
            else "WEAK"
        )
        description = (
            "پس از روند صعودی، کندل اول صعودی و بلند، کندل دوم کوچک/دوجی و کندل سوم نزولی و بلند است؛ "
            "بسته‌شدن کندل سوم دست‌کم تا نیمهٔ بدنهٔ کندل اول پایین آمده است. "
            f"گپ صعودی میان کندل اول و دوم: {'دارد' if gap_up else 'ندارد'}؛ "
            f"گپ نزولی میان کندل دوم و سوم: {'دارد' if gap_down else 'ندارد'}. "
        )
        if follow_through:
            description += "کندل بعدی زیر کف کندل سوم بسته شده و برگشت نزولی را تأیید کرده است."
        else:
            description += "برای تأیید، بسته‌شدن یک کندل بعدی زیر کف کندل سوم لازم است."

        return _item(
            name_fa="ستاره شامگاهی",
            name_en="Evening Star",
            category="CANDLE",
            direction="BEARISH" if follow_through else "NEUTRAL",
            potential_direction=None if follow_through else "BEARISH",
            state="CONFIRMED" if follow_through else "NEEDS_CONFIRMATION",
            strength=strength,
            description=description,
            timestamp=frame.index[start + 2].isoformat(),
            score=-17 if follow_through else -8,
            trigger_price=third_low,
            invalidation_price=first_high,
        )
    return None





def _shooting_star_pattern(frame: pd.DataFrame, volume_ratio: float | None) -> dict[str, Any] | None:
    """Detect a Shooting Star after an uptrend; confirm on a close below its low."""
    if len(frame) < 6:
        return None

    candidates = []
    if len(frame) >= 2:
        candidates.append((len(frame) - 2, True))
    candidates.append((len(frame) - 1, False))

    for index, has_follow_through in candidates:
        candle = frame.iloc[index]
        o, h, low, close = (
            float(candle["open"]), float(candle["high"]),
            float(candle["low"]), float(candle["close"])
        )
        candle_range = h - low
        body = abs(close - o)
        upper_shadow = h - max(o, close)
        lower_shadow = min(o, close) - low
        if candle_range <= 0 or body <= 1e-12:
            continue

        body_ratio = body / candle_range
        close_location = (close - low) / candle_range
        valid_shape = (
            body_ratio <= 0.35
            and upper_shadow >= max(body * 2.0, candle_range * 0.45)
            and lower_shadow <= max(body * 0.8, candle_range * 0.10)
            and close_location <= 0.40
        )
        if not valid_shape:
            continue

        prior = pd.to_numeric(
            frame["close"].iloc[max(0, index - 8):index], errors="coerce"
        )
        if len(prior) < 3 or prior.isna().any() or float(prior.iloc[0]) <= 0:
            continue
        prior_change = float(prior.iloc[-1] / prior.iloc[0] - 1.0)
        if prior_change < 0.003:
            continue

        follow_through = (
            has_follow_through
            and float(frame.iloc[index + 1]["close"]) < low
        )
        volume_confirmed = volume_ratio is not None and volume_ratio >= 1.20
        bearish_body = close < o
        strength = (
            "STRONG" if follow_through and volume_confirmed
            else "MODERATE" if follow_through or volume_confirmed
            else "WEAK"
        )
        description = (
            "پس از حرکت صعودی، کندلی با بدنهٔ کوچک در بخش پایینی دامنه و سایهٔ بالایی دست‌کم دو برابر بدنه تشکیل شده است؛ "
            f"بدنهٔ کندل {'نزولی' if bearish_body else 'صعودی'} است. این الگو هشدار رد قیمت‌های بالاتر است، نه دستور فروش. "
        )
        if follow_through:
            description += "کندل بعدی زیر کف ستاره ثاقب بسته شده و برگشت نزولی را تأیید کرده است."
        else:
            description += "برای تأیید نزولی، بسته‌شدن یک کندل بعدی زیر کف این الگو لازم است."

        return _item(
            name_fa="ستاره ثاقب",
            name_en="Shooting Star",
            category="CANDLE",
            direction="BEARISH" if follow_through else "NEUTRAL",
            potential_direction=None if follow_through else "BEARISH",
            state="CONFIRMED" if follow_through else "NEEDS_CONFIRMATION",
            strength=strength,
            description=description,
            timestamp=frame.index[index].isoformat(),
            score=(-18 if bearish_body else -16) if follow_through else (-11 if bearish_body else -9),
            trigger_price=low,
            invalidation_price=h,
        )
    return None


def _hanging_man_pattern(frame: pd.DataFrame, volume_ratio: float | None) -> dict[str, Any] | None:
    """Detect a Hanging Man after an uptrend and confirm on a close below its low."""
    if len(frame) < 6:
        return None

    candidates = []
    if len(frame) >= 2:
        candidates.append((len(frame) - 2, True))
    candidates.append((len(frame) - 1, False))

    for index, has_follow_through in candidates:
        candle = frame.iloc[index]
        o, h, low, close = (
            float(candle["open"]), float(candle["high"]),
            float(candle["low"]), float(candle["close"])
        )
        candle_range = h - low
        body = abs(close - o)
        upper_shadow = h - max(o, close)
        lower_shadow = min(o, close) - low
        if candle_range <= 0 or body <= 1e-12:
            continue

        body_ratio = body / candle_range
        close_location = (close - low) / candle_range
        valid_shape = (
            body_ratio <= 0.35
            and lower_shadow >= max(body * 2.0, candle_range * 0.45)
            and upper_shadow <= max(body * 0.8, candle_range * 0.10)
            and close_location >= 0.60
        )
        if not valid_shape:
            continue

        prior = pd.to_numeric(
            frame["close"].iloc[max(0, index - 8):index], errors="coerce"
        )
        if len(prior) < 3 or prior.isna().any() or float(prior.iloc[0]) <= 0:
            continue
        prior_change = float(prior.iloc[-1] / prior.iloc[0] - 1.0)
        if prior_change < 0.003:
            continue

        follow_through = (
            has_follow_through
            and float(frame.iloc[index + 1]["close"]) < low
        )
        volume_ratio_value = volume_ratio
        volume_confirmed = volume_ratio_value is not None and volume_ratio_value >= 1.20
        bearish_body = close < o
        strength = (
            "STRONG" if follow_through and volume_confirmed
            else "MODERATE" if follow_through or volume_confirmed
            else "WEAK"
        )
        description = (
            "پس از حرکت صعودی، کندلی با بدنهٔ کوچک در بخش بالایی دامنه و سایهٔ پایینی دست‌کم دو برابر بدنه تشکیل شده است؛ "
            f"بدنهٔ کندل {'نزولی' if bearish_body else 'صعودی'} است. این الگو هشدار ضعف خریداران است، نه دستور فروش. "
        )
        if follow_through:
            description += "کندل بعدی زیر کف مرد آویزان بسته شده و برگشت نزولی را تأیید کرده است."
        else:
            description += "برای تأیید نزولی، بسته‌شدن یک کندل بعدی زیر کف این الگو لازم است."

        return _item(
            name_fa="مرد آویزان",
            name_en="Hanging Man",
            category="CANDLE",
            direction="BEARISH" if follow_through else "NEUTRAL",
            potential_direction=None if follow_through else "BEARISH",
            state="CONFIRMED" if follow_through else "NEEDS_CONFIRMATION",
            strength=strength,
            description=description,
            timestamp=frame.index[index].isoformat(),
            score=(-18 if bearish_body else -16) if follow_through else (-11 if bearish_body else -9),
            trigger_price=low,
            invalidation_price=h,
        )
    return None


def _three_black_crows_pattern(frame: pd.DataFrame, volume_ratio: float | None) -> dict[str, Any] | None:
    """Detect Three Black Crows after a bullish phase and require follow-through to confirm."""
    if len(frame) < 6:
        return None

    candidates = []
    if len(frame) >= 4:
        candidates.append((len(frame) - 4, True))
    candidates.append((len(frame) - 3, False))

    for start, has_follow_through in candidates:
        candles = [frame.iloc[start + offset] for offset in range(3)]
        prior = pd.to_numeric(frame["close"].iloc[max(0, start - 8):start], errors="coerce")
        if len(prior) < 3 or prior.isna().any() or float(prior.iloc[0]) <= 0:
            continue
        prior_change = float(prior.iloc[-1] / prior.iloc[0] - 1.0)
        # The pattern is a reversal warning, so require a preceding bullish phase.
        if prior_change < 0.003:
            continue

        values = []
        for candle in candles:
            o, h, low, close = (
                float(candle["open"]), float(candle["high"]),
                float(candle["low"]), float(candle["close"])
            )
            candle_range = h - low
            body = o - close
            upper_shadow = h - o
            if candle_range <= 0:
                values = []
                break
            values.append((o, h, low, close, candle_range, body, upper_shadow))
        if len(values) != 3:
            continue

        first, second, third = values
        bearish_long = all(body > 0 and body >= candle_range * 0.50
                           for _, _, _, _, candle_range, body, _ in values)
        upper_wicks_short = all(
            upper_shadow <= min(body * 0.25, candle_range * 0.10) + 1e-12
            for _, _, _, _, candle_range, body, upper_shadow in values
        )
        opens_within_previous_body = (
            first[3] <= second[0] <= first[0]
            and second[3] <= third[0] <= second[0]
        )
        descending_closes = first[3] > second[3] > third[3]
        if not (bearish_long and upper_wicks_short and opens_within_previous_body and descending_closes):
            continue

        first_open, first_high = first[0], first[1]
        third_low = third[2]
        follow_through = (
            has_follow_through
            and float(frame.iloc[start + 3]["close"]) < third_low
        )
        volume_confirmed = volume_ratio is not None and volume_ratio >= 1.20
        strength = (
            "STRONG" if follow_through and volume_confirmed
            else "MODERATE" if follow_through or volume_confirmed
            else "WEAK"
        )
        description = (
            "پس از فاز صعودی، سه کندل نزولیِ بلند با بسته‌شدن‌های پیاپی پایین‌تر تشکیل شده‌اند؛ "
            "بازشدن کندل دوم و سوم داخل بدنهٔ کندل قبلی بوده و سایه‌های بالایی کوتاه‌اند. "
        )
        if follow_through:
            description += "کندل بعدی زیر کف کندل سوم بسته شده و ادامهٔ فشار فروش را تأیید کرده است."
        else:
            description += "الگو فعلاً هشدار برگشت نزولی است؛ برای تأیید، بسته‌شدن کندل بعدی زیر کف کندل سوم لازم است."

        return _item(
            name_fa="سه کلاغ سیاه",
            name_en="Three Black Crows",
            category="CANDLE",
            direction="BEARISH" if follow_through else "NEUTRAL",
            potential_direction=None if follow_through else "BEARISH",
            state="CONFIRMED" if follow_through else "NEEDS_CONFIRMATION",
            strength=strength,
            description=description,
            timestamp=frame.index[start + 2].isoformat(),
            score=-17 if follow_through else -8,
            trigger_price=third_low,
            invalidation_price=first_high,
        )
    return None


def _three_inside_up_pattern(frame: pd.DataFrame, volume_ratio: float | None) -> dict[str, Any] | None:
    """Detect Three Inside Up: bearish candle, bullish Harami, bullish breakout."""
    if len(frame) < 6:
        return None

    first, second, third = (frame.iloc[-3 + i] for i in range(3))
    prior = pd.to_numeric(frame["close"].iloc[-11:-3], errors="coerce")
    if len(prior) < 3 or prior.isna().any() or float(prior.iloc[0]) <= 0:
        return None
    prior_change = float(prior.iloc[-1] / prior.iloc[0] - 1.0)
    if prior_change > -0.003:
        return None

    fo, fc = float(first["open"]), float(first["close"])
    fh, fl = float(first["high"]), float(first["low"])
    so, sc = float(second["open"]), float(second["close"])
    sh, sl = float(second["high"]), float(second["low"])
    to, tc = float(third["open"]), float(third["close"])
    th, tl = float(third["high"]), float(third["low"])

    first_body = abs(fo - fc)
    first_range = fh - fl
    second_body = abs(sc - so)
    second_range = sh - sl
    third_body = abs(tc - to)
    third_range = th - tl
    if min(first_range, second_range, third_range) <= 0:
        return None

    first_is_long_bearish = fc < fo and first_body >= first_range * 0.45
    bullish_harami = (
        sc > so
        and second_body > 0
        and second_body <= first_body * 0.60
        and so >= fc
        and sc <= fo
        and sh <= fh
        and sl >= fl
    )
    third_is_bullish = tc > to and third_body >= third_range * 0.30
    if not (first_is_long_bearish and bullish_harami and third_is_bullish):
        return None

    confirmed = tc > fh
    volume_confirmed = volume_ratio is not None and volume_ratio >= 1.20
    state = "CONFIRMED" if confirmed else "NEEDS_CONFIRMATION"
    strength = (
        "STRONG" if confirmed and volume_confirmed
        else "MODERATE" if confirmed or volume_confirmed
        else "WEAK"
    )
    description = (
        "پس از روند نزولی، کندل اول قرمز و بلند است؛ کندل دوم سبز و کوچک‌تر، "
        "در محدودهٔ بدنهٔ کندل اول قرار دارد و ساختار هارامی صعودی می‌سازد. "
    )
    if confirmed:
        description += "کندل سوم بالاتر از سقف کندل اول بسته شده و بازگشت صعودی تأیید شده است."
    else:
        description += "کندل سوم صعودی است اما هنوز بالای سقف کندل اول بسته نشده؛ تأیید کامل نشده است."
    return _item(
        name_fa="سه‌تایی از داخل به بالا",
        name_en="Three Inside Up",
        category="CANDLE",
        direction="BULLISH" if confirmed else "NEUTRAL",
        potential_direction=None if confirmed else "BULLISH",
        state=state,
        strength=strength,
        description=description,
        timestamp=frame.index[-1].isoformat(),
        score=15 if confirmed else 7,
        trigger_price=fh,
        invalidation_price=fl,
    )


def _bearish_engulfing_pattern(frame: pd.DataFrame, volume_ratio: float | None) -> dict[str, Any] | None:
    """Detect bearish body engulfing after an advance; next close confirms it."""
    if len(frame) < 4:
        return None

    candidates = []
    if len(frame) >= 3:
        candidates.append((len(frame) - 3, True))
    candidates.append((len(frame) - 2, False))

    for start, has_follow_through in candidates:
        previous, engulfing = frame.iloc[start], frame.iloc[start + 1]
        prior = pd.to_numeric(frame["close"].iloc[max(0, start - 8):start], errors="coerce")
        if len(prior) < 3 or prior.isna().any() or float(prior.iloc[0]) <= 0:
            continue
        prior_change = float(prior.iloc[-1] / prior.iloc[0] - 1.0)
        if prior_change < 0.003:
            continue

        po, pc = float(previous["open"]), float(previous["close"])
        eo, ec = float(engulfing["open"]), float(engulfing["close"])
        eh, el = float(engulfing["high"]), float(engulfing["low"])
        previous_body = abs(pc - po)
        engulfing_body = abs(eo - ec)
        if not (
            pc > po
            and ec < eo
            and previous_body > 0
            and engulfing_body > previous_body
            and eo >= pc
            and ec <= po
        ):
            continue

        follow_through = (
            has_follow_through
            and float(frame.iloc[start + 2]["close"]) < el
        )
        volume_confirmed = volume_ratio is not None and volume_ratio >= 1.20
        shadow_engulf = eh >= float(previous["high"]) and el <= float(previous["low"])
        strength = (
            "STRONG" if follow_through and volume_confirmed
            else "MODERATE" if follow_through or volume_confirmed
            else "WEAK"
        )
        description = (
            "پس از فاز صعودی، کندل دوم نزولی و بزرگ‌تر است و تمام بدنهٔ کندل صعودی قبلی را می‌پوشاند. "
            f"پوشش کامل سایه‌ها: {'بله' if shadow_engulf else 'خیر؛ پوشش سایه‌ها شرط اصلی نیست'}. "
        )
        if follow_through:
            description += "کندل بعدی زیر کف کندل پوشا بسته شده و برگشت نزولی را تأیید کرده است."
        else:
            description += "برای تأیید، بسته‌شدن کندل بعدی زیر کف کندل پوشا لازم است."

        return _item(
            name_fa="پوشای نزولی",
            name_en="Bearish Engulfing",
            category="CANDLE",
            direction="BEARISH" if follow_through else "NEUTRAL",
            potential_direction=None if follow_through else "BEARISH",
            state="CONFIRMED" if follow_through else "NEEDS_CONFIRMATION",
            strength=strength,
            description=description,
            timestamp=frame.index[start + 1].isoformat(),
            score=-15 if follow_through else -7,
            trigger_price=el,
            invalidation_price=eh,
        )
    return None


def _bullish_engulfing_pattern(frame: pd.DataFrame, volume_ratio: float | None) -> dict[str, Any] | None:
    """Detect bullish body engulfing after a decline; next close confirms the setup."""
    if len(frame) < 4:
        return None

    candidates = []
    if len(frame) >= 3:
        candidates.append((len(frame) - 3, True))
    candidates.append((len(frame) - 2, False))

    for start, has_follow_through in candidates:
        previous, engulfing = frame.iloc[start], frame.iloc[start + 1]
        prior = pd.to_numeric(frame["close"].iloc[max(0, start - 8):start], errors="coerce")
        if len(prior) < 3 or prior.isna().any() or float(prior.iloc[0]) <= 0:
            continue
        prior_change = float(prior.iloc[-1] / prior.iloc[0] - 1.0)
        if prior_change > -0.003:
            continue

        po, pc = float(previous["open"]), float(previous["close"])
        eo, ec = float(engulfing["open"]), float(engulfing["close"])
        eh, el = float(engulfing["high"]), float(engulfing["low"])
        previous_body = abs(po - pc)
        engulfing_body = abs(ec - eo)
        if not (
            pc < po
            and ec > eo
            and previous_body > 0
            and engulfing_body > previous_body
            and eo <= pc
            and ec >= po
        ):
            continue

        follow_through = (
            has_follow_through
            and float(frame.iloc[start + 2]["close"]) > eh
        )
        volume_confirmed = volume_ratio is not None and volume_ratio >= 1.20
        state = "CONFIRMED" if follow_through else "NEEDS_CONFIRMATION"
        strength = (
            "STRONG" if follow_through and volume_confirmed
            else "MODERATE" if follow_through or volume_confirmed
            else "WEAK"
        )
        shadow_engulf = (
            float(engulfing["high"]) >= float(previous["high"])
            and el <= float(previous["low"])
        )
        description = (
            "پس از افت قیمت، کندل سبز دوم تمام بدنهٔ کندل قرمز قبلی را پوشانده است. "
            f"پوشش سایه‌ها: {'کامل' if shadow_engulf else 'ناقص یا غیرضروری'}."
        )
        if follow_through:
            description += " کندل بعدی بالاتر از سقف کندل پوشا بسته شده و الگو تأیید شده است."
        else:
            description += " برای تأیید، بسته‌شدن کندل بعدی بالاتر از سقف کندل پوشا لازم است."
        return _item(
            name_fa="پوشای صعودی",
            name_en="Bullish Engulfing",
            category="CANDLE",
            direction="BULLISH" if follow_through else "NEUTRAL",
            potential_direction=None if follow_through else "BULLISH",
            state=state,
            strength=strength,
            description=description,
            timestamp=frame.index[start + 1].isoformat(),
            score=14 if follow_through else 7,
            trigger_price=eh,
            invalidation_price=el,
        )
    return None


def _bearish_three_line_strike_pattern(frame: pd.DataFrame, volume_ratio: float | None) -> dict[str, Any] | None:
    """Detect the four-candle bearish continuation Three Line Strike."""
    if len(frame) < 7:
        return None

    candidates = []
    if len(frame) >= 5:
        candidates.append((len(frame) - 5, True))
    candidates.append((len(frame) - 4, False))

    for start, has_follow_through in candidates:
        first, second, third, strike = (frame.iloc[start + i] for i in range(4))
        prior = pd.to_numeric(frame["close"].iloc[max(0, start - 8):start], errors="coerce")
        if len(prior) < 3 or prior.isna().any() or float(prior.iloc[0]) <= 0:
            continue
        prior_change = float(prior.iloc[-1] / prior.iloc[0] - 1.0)
        if prior_change > -0.003:
            continue

        candles = (first, second, third)
        bodies = [float(x["open"]) - float(x["close"]) for x in candles]
        ranges = [float(x["high"]) - float(x["low"]) for x in candles]
        strong_declines = all(
            body > 0 and candle_range > 0 and body >= candle_range * 0.40
            for body, candle_range in zip(bodies, ranges)
        )
        descending_closes = (
            float(second["close"]) < float(first["close"])
            and float(third["close"]) < float(second["close"])
        )
        close_near_lows = all(
            float(x["close"]) - float(x["low"]) <= max(body * 0.35, 1e-12)
            for x, body in zip(candles, bodies)
        )

        strike_open = float(strike["open"])
        strike_close = float(strike["close"])
        strike_high = float(strike["high"])
        strike_low = float(strike["low"])
        engulfed_body_top = max(float(x["open"]) for x in candles)
        engulfed_body_bottom = min(float(x["close"]) for x in candles)
        strike_valid = (
            strike_close > strike_open
            and strike_open < float(third["close"])
            and strike_open <= engulfed_body_bottom
            and strike_close > float(first["open"])
            and strike_close >= engulfed_body_top
        )
        if not (strong_declines and descending_closes and close_near_lows and strike_valid):
            continue

        follow_through = (
            has_follow_through
            and float(frame.iloc[start + 4]["close"]) < strike_low
        )
        volume_confirmed = volume_ratio is not None and volume_ratio >= 1.20
        state = "CONFIRMED" if follow_through else "NEEDS_CONFIRMATION"
        strength = (
            "STRONG" if follow_through and volume_confirmed
            else "MODERATE" if follow_through or volume_confirmed
            else "WEAK"
        )
        description = (
            "الگوی ادامه‌دهندهٔ نزولی چهارکندلی: سه کندل قرمز با بسته‌شدن‌های پیاپی پایین‌تر و نزدیک کف، "
            "سپس یک کندل سبز بلند که کل بدنهٔ سه کندل قبلی را می‌پوشاند و بالاتر از بازشدن کندل اول بسته می‌شود. "
            "پوشش سایه‌ها شرط نیست."
        )
        if follow_through:
            description += " کندل بعدی زیر کف کندل چهارم بسته شده و ادامهٔ نزول را تأیید کرده است."
        else:
            description += " برای تأیید ادامهٔ نزول، بسته‌شدن کندل بعدی زیر کف کندل چهارم لازم است."
        return _item(
            name_fa="ضربهٔ سه‌خطی نزولی",
            name_en="Bearish Three Line Strike",
            category="CANDLE",
            direction="BEARISH" if follow_through else "NEUTRAL",
            potential_direction=None if follow_through else "BEARISH",
            state=state,
            strength=strength,
            description=description,
            timestamp=frame.index[start + 3].isoformat(),
            score=-16 if follow_through else -8,
            trigger_price=strike_low,
            invalidation_price=strike_high,
        )
    return None


def _bullish_three_line_strike_pattern(frame: pd.DataFrame, volume_ratio: float | None) -> dict[str, Any] | None:
    """Detect the four-candle bullish continuation Three Line Strike."""
    if len(frame) < 7:
        return None

    candidates = []
    if len(frame) >= 5:
        candidates.append((len(frame) - 5, True))
    candidates.append((len(frame) - 4, False))

    for start, has_follow_through in candidates:
        first, second, third, strike = (frame.iloc[start + i] for i in range(4))
        prior = pd.to_numeric(frame["close"].iloc[max(0, start - 8):start], errors="coerce")
        if len(prior) < 3 or prior.isna().any() or float(prior.iloc[0]) <= 0:
            continue
        prior_change = float(prior.iloc[-1] / prior.iloc[0] - 1.0)
        if prior_change < 0.003:
            continue

        bullish = (first, second, third)
        bodies = [float(x["close"]) - float(x["open"]) for x in bullish]
        ranges = [float(x["high"]) - float(x["low"]) for x in bullish]
        strong_advances = all(
            body > 0 and candle_range > 0 and body >= candle_range * 0.40
            for body, candle_range in zip(bodies, ranges)
        )
        rising_closes = (
            float(second["close"]) > float(first["close"])
            and float(third["close"]) > float(second["close"])
        )
        close_near_highs = all(
            float(x["high"]) - float(x["close"]) <= max(body * 0.35, 1e-12)
            for x, body in zip(bullish, bodies)
        )

        strike_open = float(strike["open"])
        strike_close = float(strike["close"])
        strike_high = float(strike["high"])
        strike_low = float(strike["low"])
        engulfed_body_top = max(float(x["close"]) for x in bullish)
        engulfed_body_bottom = min(float(x["open"]) for x in bullish)
        strike_valid = (
            strike_close < strike_open
            and strike_open > float(third["close"])
            and strike_open >= engulfed_body_top
            and strike_close < float(first["open"])
            and strike_close <= engulfed_body_bottom
        )
        if not (strong_advances and rising_closes and close_near_highs and strike_valid):
            continue

        follow_through = (
            has_follow_through
            and float(frame.iloc[start + 4]["close"]) > strike_high
        )
        volume_confirmed = volume_ratio is not None and volume_ratio >= 1.20
        state = "CONFIRMED" if follow_through else "NEEDS_CONFIRMATION"
        strength = (
            "STRONG" if follow_through and volume_confirmed
            else "MODERATE" if follow_through or volume_confirmed
            else "WEAK"
        )
        description = (
            "الگوی ادامه‌دهندهٔ صعودی چهارکندلی: سه کندل سبز با بسته‌شدن‌های بالاتر، "
            "سپس یک کندل قرمز بلند که بدنهٔ هر سه کندل را پوشانده و زیر بازشدن کندل اول بسته شده است. "
            "پوشش سایه‌ها شرط نیست."
        )
        if follow_through:
            description += " کندل بعدی بالاتر از سقف کندل چهارم بسته شده و ادامهٔ صعود تأیید شده است."
        else:
            description += " تا بسته‌شدن کندل بعدی بالاتر از سقف کندل چهارم، الگو در انتظار تأیید است."
        return _item(
            name_fa="ضربهٔ سه‌خطی صعودی",
            name_en="Bullish Three Line Strike",
            category="CANDLE",
            direction="BULLISH" if follow_through else "NEUTRAL",
            potential_direction=None if follow_through else "BULLISH",
            state=state,
            strength=strength,
            description=description,
            timestamp=frame.index[start + 3].isoformat(),
            score=16 if follow_through else 8,
            trigger_price=strike_high,
            invalidation_price=strike_low,
        )
    return None


def _bullish_marubozu_pattern(frame: pd.DataFrame, volume_ratio: float | None) -> dict[str, Any] | None:
    """Detect a long bullish near-shadeless candle; a later close above its high confirms."""
    if len(frame) < 6:
        return None

    candidates = []
    if len(frame) >= 2:
        candidates.append((len(frame) - 2, True))
    candidates.append((len(frame) - 1, False))

    for index, has_follow_through in candidates:
        candle = frame.iloc[index]
        prior = frame.iloc[max(0, index - 8):index]
        if len(prior) < 3:
            continue
        prior_closes = pd.to_numeric(prior["close"], errors="coerce")
        prior_bodies = (prior["close"].astype(float) - prior["open"].astype(float)).abs()
        if prior_closes.isna().any() or prior_bodies.isna().any():
            continue

        o, h, low, c = (float(candle[k]) for k in ("open", "high", "low", "close"))
        candle_range = h - low
        body = c - o
        upper_shadow = h - c
        lower_shadow = o - low
        median_prior_body = float(prior_bodies.median())
        if candle_range <= 0 or median_prior_body <= 0:
            continue

        valid = (
            body > 0
            and body >= candle_range * 0.85
            and upper_shadow >= -1e-12
            and lower_shadow >= -1e-12
            and upper_shadow <= candle_range * 0.05
            and lower_shadow <= candle_range * 0.05
            and body >= median_prior_body * 1.5
        )
        if not valid:
            continue

        prior_change = float(prior_closes.iloc[-1] / prior_closes.iloc[0] - 1.0)
        context = "DOWN" if prior_change <= -0.003 else "UP" if prior_change >= 0.003 else "SIDEWAYS"
        confirmed = has_follow_through and float(frame.iloc[index + 1]["close"]) > h
        volume_confirmed = volume_ratio is not None and volume_ratio >= 1.20
        strength = (
            "STRONG" if confirmed and volume_confirmed
            else "MODERATE" if confirmed or volume_confirmed
            else "WEAK"
        )
        description = (
            "کندل صعودی با بدنه‌ای بلند و سایه‌های بسیار کوتاه تشکیل شده است؛ "
            "قیمت تقریباً از کف باز شده و نزدیک سقف بسته شده، که نشان‌دهندهٔ فشار خرید قوی در این بازه است. "
        )
        if context == "DOWN":
            description += "این حرکت پس از افت اخیر می‌تواند نشانهٔ تلاش برای برگشت باشد."
        elif context == "UP":
            description += "در روند صعودی، می‌تواند نشانهٔ ادامهٔ فشار خرید باشد؛ پس از رشد طولانی خطر خستگی روند نیز باید بررسی شود."
        else:
            description += "روند پیشین خنثی است و از این کندل به‌تنهایی نمی‌توان برگشت روند را نتیجه گرفت."
        if confirmed:
            description += " کندل بعدی بالاتر از سقف ماروبوزو بسته شده و حرکت صعودی تأیید شده است."
        else:
            description += " برای تأیید، بسته‌شدن کندل بعدی بالاتر از سقف ماروبوزو لازم است."
        return _item(
            name_fa="ماروبوزوی صعودی",
            name_en="Bullish Marubozu",
            category="CANDLE",
            direction="BULLISH" if confirmed else "NEUTRAL",
            potential_direction=None if confirmed else "BULLISH",
            state="CONFIRMED" if confirmed else "NEEDS_CONFIRMATION",
            strength=strength,
            description=description,
            timestamp=frame.index[index].isoformat(),
            score=14 if confirmed else 7,
            trigger_price=h,
            invalidation_price=low,
        )
    return None

def _piercing_line_pattern(frame: pd.DataFrame, volume_ratio: float | None) -> dict[str, Any] | None:
    """Detect a two-candle Piercing Line after a decline; next close confirms it."""
    if len(frame) < 5:
        return None

    candidates = []
    if len(frame) >= 3:
        candidates.append((len(frame) - 3, True))
    candidates.append((len(frame) - 2, False))

    for start, has_follow_through in candidates:
        first, second = frame.iloc[start], frame.iloc[start + 1]
        prior = pd.to_numeric(
            frame["close"].iloc[max(0, start - 8):start], errors="coerce"
        )
        if len(prior) < 3 or prior.isna().any() or float(prior.iloc[0]) <= 0:
            continue
        prior_change = float(prior.iloc[-1] / prior.iloc[0] - 1.0)
        if prior_change > -0.003:
            continue

        first_open, first_close = float(first["open"]), float(first["close"])
        first_high, first_low = float(first["high"]), float(first["low"])
        second_open, second_close = float(second["open"]), float(second["close"])
        second_high, second_low = float(second["high"]), float(second["low"])
        first_range = first_high - first_low
        second_range = second_high - second_low
        first_body = first_open - first_close
        midpoint = (first_open + first_close) / 2.0

        valid = (
            first_range > 0
            and second_range > 0
            and first_close < first_open
            and first_body >= first_range * 0.45
            and second_close > second_open
            and second_open < first_low
            and second_close > midpoint
            and second_close < first_open
        )
        if not valid:
            continue

        confirmed = (
            has_follow_through
            and float(frame.iloc[start + 2]["close"]) > second_high
        )
        volume_confirmed = volume_ratio is not None and volume_ratio >= 1.20
        state = "CONFIRMED" if confirmed else "NEEDS_CONFIRMATION"
        strength = (
            "STRONG" if confirmed and volume_confirmed
            else "MODERATE" if confirmed or volume_confirmed
            else "WEAK"
        )
        description = (
            "پس از روند نزولی، کندل اول نزولی و نسبتاً بلند است. کندل دوم زیر کف کندل اول باز شده، "
            "اما صعودی بسته شده و بیش از نیمهٔ بدنهٔ کندل اول را پس گرفته است؛ "
            "بسته‌شدن آن هنوز پایین‌تر از قیمت بازشدن کندل اول است. "
        )
        if confirmed:
            description += "کندل بعدی بالاتر از سقف کندل دوم بسته شده و الگو تأیید شده است."
        else:
            description += "الگو هنوز در انتظار تأیید است؛ بسته‌شدن کندل بعدی بالاتر از سقف کندل دوم لازم است."

        return _item(
            name_fa="خط نفوذی صعودی",
            name_en="Piercing Line",
            category="CANDLE",
            direction="BULLISH" if confirmed else "NEUTRAL",
            potential_direction=None if confirmed else "BULLISH",
            state=state,
            strength=strength,
            description=description,
            timestamp=frame.index[start + 1].isoformat(),
            score=13 if confirmed else 6,
            trigger_price=second_high,
            invalidation_price=second_low,
        )
    return None


def _candle_patterns(frame: pd.DataFrame, volume_ratio: float | None) -> list[dict[str, Any]]:
    if len(frame) < 3:
        return []
    bars = frame.tail(3)
    first, middle, last = bars.iloc[-3], bars.iloc[-2], bars.iloc[-1]
    o, h, low, c = (float(last[k]) for k in ("open", "high", "low", "close"))
    fo, fh, fl, fc = (float(first[k]) for k in ("open", "high", "low", "close"))
    po, ph, pl, pc = (float(middle[k]) for k in ("open", "high", "low", "close"))
    mo, mh, ml, mc = po, ph, pl, pc
    body = abs(c - o)
    candle_range = max(h - low, 1e-15)
    upper = h - max(o, c)
    lower = min(o, c) - low
    close_location = (c - low) / candle_range
    body_ratio = body / candle_range
    prior = frame["close"].iloc[-9:-1]
    prior_change = (
        float(prior.iloc[-1] / prior.iloc[0] - 1.0)
        if len(prior) >= 2 and float(prior.iloc[0]) > 0 else 0.0
    )
    context = "DOWN" if prior_change <= -0.003 else "UP" if prior_change >= 0.003 else "SIDEWAYS"
    volume_confirmed = volume_ratio is not None and volume_ratio >= 1.20
    timestamp = frame.index[-1].isoformat()
    out: list[dict[str, Any]] = []

    def add(name_fa: str, name_en: str, direction: str, description: str, score: int,
            *, state: str = "NEEDS_CONFIRMATION", strength: str | None = None) -> None:
        actual_strength = strength or (
            "STRONG" if volume_confirmed and abs(score) >= 12 else
            "MODERATE" if abs(score) >= 8 else "WEAK"
        )
        out.append(_item(
            name_fa=name_fa, name_en=name_en, category="CANDLE",
            direction=direction, state=state, strength=actual_strength,
            description=description, timestamp=timestamp, score=score,
            trigger_price=h if direction == "BULLISH" else low if direction == "BEARISH" else None,
            invalidation_price=low if direction == "BULLISH" else h if direction == "BEARISH" else None,
        ))

    # Three-candle reversal families take priority over single-candle shapes.
    first_body = abs(fc - fo)
    first_mid = (fo + fc) / 2.0
    middle_body = abs(float(middle["close"]) - float(middle["open"]))
    last_bull = c > o
    last_bear = c < o
    first_bear = fc < fo
    first_bull = fc > fo


    bearish_abandoned_baby = _bearish_abandoned_baby_pattern(frame, volume_ratio)
    if bearish_abandoned_baby is not None:
        out.append(bearish_abandoned_baby)

    abandoned_baby = _bullish_abandoned_baby_pattern(frame, volume_ratio)
    if abandoned_baby is not None:
        out.append(abandoned_baby)

    rising_window = _rising_window_pattern(frame, volume_ratio)
    if rising_window is not None:
        out.append(rising_window)

    morning_star = _morning_star_pattern(frame, volume_ratio)
    if morning_star is not None:
        out.append(morning_star)

    three_inside_up = _three_inside_up_pattern(frame, volume_ratio)
    if three_inside_up is not None:
        out.append(three_inside_up)

    bullish_engulfing = _bullish_engulfing_pattern(frame, volume_ratio)
    if bullish_engulfing is not None:
        out.append(bullish_engulfing)

    three_line_strike = _bullish_three_line_strike_pattern(frame, volume_ratio)
    if three_line_strike is not None:
        out.append(three_line_strike)

    bearish_three_line_strike = _bearish_three_line_strike_pattern(frame, volume_ratio)
    if bearish_three_line_strike is not None:
        out.append(bearish_three_line_strike)

    bearish_engulfing = _bearish_engulfing_pattern(frame, volume_ratio)
    if bearish_engulfing is not None:
        out.append(bearish_engulfing)

    bullish_marubozu = _bullish_marubozu_pattern(frame, volume_ratio)
    if bullish_marubozu is not None:
        out.append(bullish_marubozu)

    piercing_line = _piercing_line_pattern(frame, volume_ratio)
    if piercing_line is not None:
        out.append(piercing_line)

    if pc > po and last_bear and o > ph and c < (po + pc) / 2.0 and c > po:
        add("ابر تیره", "Dark Cloud Cover", "BEARISH",
            "کندل نزولی بیش از نیمهٔ بدنهٔ صعودی قبلی را پس گرفته است.", -10)

    # Three White Soldiers: three strong advances with controlled pullbacks.
    three = frame.tail(3)
    soldiers = three.to_dict("records")
    soldier_bodies = [float(x["close"]) - float(x["open"]) for x in soldiers]
    soldier_ranges = [float(x["high"]) - float(x["low"]) for x in soldiers]
    soldier_lower = [min(float(x["open"]), float(x["close"])) - float(x["low"]) for x in soldiers]
    soldier_upper = [float(x["high"]) - max(float(x["open"]), float(x["close"])) for x in soldiers]
    prior_soldier_closes = pd.to_numeric(frame["close"].iloc[-11:-3], errors="coerce")
    prior_move = (
        float(prior_soldier_closes.iloc[-1] / prior_soldier_closes.iloc[0] - 1.0)
        if len(prior_soldier_closes) >= 3 and float(prior_soldier_closes.iloc[0]) > 0
        else 0.0
    )
    prior_not_bullish = prior_move <= 0.003
    opens_inside_previous_bodies = all(
        min(float(soldiers[i - 1]["open"]), float(soldiers[i - 1]["close"]))
        <= float(soldiers[i]["open"])
        <= max(float(soldiers[i - 1]["open"]), float(soldiers[i - 1]["close"]))
        for i in (1, 2)
    )
    soldiers_valid = (
        prior_not_bullish
        and all(r > 0 for r in soldier_ranges)
        and all(b > 0 and b >= r * 0.50 for b, r in zip(soldier_bodies, soldier_ranges))
        and all(w <= b * 0.25 for w, b in zip(soldier_lower, soldier_bodies))
        and all(w <= b * 0.50 for w, b in zip(soldier_upper, soldier_bodies))
        and all(soldiers[i]["close"] > soldiers[i - 1]["close"] for i in (1, 2))
        and opens_inside_previous_bodies
    )
    if soldiers_valid:
        add("سه سرباز سفید", "Three White Soldiers", "BULLISH",
            "سه کندل سبزِ قوی پس از روند نزولی/تثبیت؛ هر کندل داخل بدنه قبلی باز شده، بالاتر بسته شده و سایه پایینی کوتاه دارد. برای ورود، تأیید مستقل روند و ریسک لازم است.",
            15, strength="STRONG" if volume_confirmed else "MODERATE")
    # Single-candle reversal shapes; trend context distinguishes hammer from hanging man.
    small_body = body_ratio <= 0.35
    long_lower = lower >= max(body * 2.0, candle_range * 0.45)
    long_upper = upper >= max(body * 2.0, candle_range * 0.45)
    short_upper = upper <= max(body * 0.8, candle_range * 0.10)
    short_lower = lower <= max(body * 0.8, candle_range * 0.10)
    if small_body and long_lower and short_upper and close_location >= 0.60:
        if context == "DOWN":
            add("چکش صعودی", "Hammer", "BULLISH",
                "سایهٔ پایینی بلند و بسته‌شدن نزدیک سقف پس از افت اخیر؛ نیازمند تأیید صعودی کندل بعدی.", 11)
        elif context == "UP":
            # Hanging Man is emitted by the confirmation-aware detector below.
            pass
        else:
            add("چکش‌مانند", "Hammer-like", "NEUTRAL",
                "شکل چکشی دیده شده اما روند پیشین برای تفسیر جهت‌دار روشن نیست.", 0)
    elif small_body and long_upper and short_lower and close_location <= 0.40:
        if context == "DOWN":
            add("چکش معکوس", "Inverted Hammer", "BULLISH",
                "سایهٔ بالایی بلند پس از افت؛ بازگشت فقط با تأیید کندل بعدی معتبرتر می‌شود.", 9)
        elif context == "UP":
            # Shooting Star is emitted by the confirmation-aware detector below.
            pass
        else:
            add("ستاره معکوس خنثی", "Inverted Star", "NEUTRAL",
                "سایهٔ بالایی بلند بدون زمینهٔ روند مشخص؛ جهت برگشت هنوز معلوم نیست.", 0)

    # Dragonfly Doji: detect the latest shape, or confirm the prior completed shape
    # when the following candle closes above its high.
    dragonfly_candidates = [(len(frame) - 1, False)]
    if len(frame) >= 2:
        dragonfly_candidates.append((len(frame) - 2, True))
    for dragonfly_index, has_follow_through in dragonfly_candidates:
        dragonfly = frame.iloc[dragonfly_index]
        d_open, d_high = float(dragonfly["open"]), float(dragonfly["high"])
        d_low, d_close = float(dragonfly["low"]), float(dragonfly["close"])
        d_body = abs(d_close - d_open)
        d_range = d_high - d_low
        d_upper = d_high - max(d_open, d_close)
        d_lower = min(d_open, d_close) - d_low
        d_close_location = (d_close - d_low) / max(d_range, 1e-15)
        d_body_ratio = d_body / max(d_range, 1e-15)
        is_dragonfly = (
            d_range > 0
            and d_body_ratio <= 0.10
            and d_lower >= d_range * 0.60
            and d_lower >= max(d_body * 2.0, d_range * 0.60)
            and d_upper <= max(d_body * 0.8, d_range * 0.08)
            and d_close_location >= 0.85
        )
        if not is_dragonfly:
            continue
        prior_closes = pd.to_numeric(
            frame["close"].iloc[max(0, dragonfly_index - 8):dragonfly_index], errors="coerce"
        )
        prior_change = (
            float(prior_closes.iloc[-1] / prior_closes.iloc[0] - 1.0)
            if len(prior_closes) >= 3 and not prior_closes.isna().any()
            and float(prior_closes.iloc[0]) > 0 else 0.0
        )
        contextual = prior_change <= -0.003
        confirmed = (
            has_follow_through
            and float(frame.iloc[dragonfly_index + 1]["close"]) > d_high
        )
        out.append(_item(
            name_fa="دوجی سنجاقک",
            name_en="Dragonfly Doji",
            category="CANDLE",
            direction="BULLISH" if confirmed else "NEUTRAL",
            potential_direction="BULLISH" if contextual and not confirmed else None,
            state="CONFIRMED" if confirmed else "NEEDS_CONFIRMATION",
            strength="STRONG" if confirmed and volume_confirmed else "MODERATE" if confirmed or volume_confirmed else "WEAK",
            description=(
                "دوجی سنجاقک با بدنهٔ بسیار کوچک نزدیک سقف و سایهٔ پایینی بلند شناسایی شد. "
                + ("کندل بعدی بالاتر از سقف دوجی بسته شده و بازگشت صعودی تأیید شده است."
                   if confirmed else
                   "پس از روند نزولی می‌تواند نشانهٔ بازگشت احتمالی صعودی باشد؛ "
                   "تا بسته‌شدن کندل بعدی بالاتر از سقف دوجی، تأیید نشده است."
                   if contextual else
                   "روند نزولی قبلی وجود ندارد؛ جهت برگشت روشن نیست و این الگو به‌تنهایی سیگنال معامله نیست.")
            ),
            timestamp=frame.index[dragonfly_index].isoformat(),
            score=12 if confirmed else 6 if contextual else 0,
            trigger_price=d_high,
            invalidation_price=d_low,
        ))
        break

    if body_ratio <= 0.10 and (long_lower == long_upper):
        add("دوجی", "Doji", "NEUTRAL",
            "بدنه بسیار کوچک نسبت به دامنهٔ کندل؛ نشان‌دهندهٔ تردید است، نه سیگنال مستقل خرید یا فروش.", 0, strength="MODERATE")
    elif body_ratio <= 0.30 and upper >= candle_range * 0.20 and lower >= candle_range * 0.20:
        add("اسپینینگ‌تاپ", "Spinning Top", "NEUTRAL",
            "بدنه کوچک و دو سایهٔ قابل‌توجه؛ تعادل موقت خریدار و فروشنده.", 0, strength="WEAK")

    if h < ph and low > pl:
        add("کندل داخلی", "Inside Bar", "NEUTRAL",
            "دامنهٔ کندل آخر داخل دامنهٔ کندل قبلی است؛ شکست هر طرف می‌تواند جهت حرکت بعدی را روشن کند.", 0, strength="WEAK")

    # Prioritize these reversal formations in the five-pattern dashboard summary.
    evening_star = _evening_star_pattern(frame, volume_ratio)
    if evening_star is not None:
        out.append(evening_star)

    three_black_crows = _three_black_crows_pattern(frame, volume_ratio)
    if three_black_crows is not None:
        out.append(three_black_crows)

    hanging_man = _hanging_man_pattern(frame, volume_ratio)
    if hanging_man is not None:
        out.append(hanging_man)

    shooting_star = _shooting_star_pattern(frame, volume_ratio)
    if shooting_star is not None:
        out.append(shooting_star)

    return out[-5:]


def _geometric_patterns(
    frame: pd.DataFrame,
    pivots: list[dict[str, Any]],
    atr: float | None,
) -> list[dict[str, Any]]:
    if not pivots or len(frame) < 30:
        return []
    latest_close = float(frame["close"].iloc[-1])
    latest_time = frame.index[-1].isoformat()
    price = max(latest_close, 1e-15)
    tolerance = max(price * 0.012, (atr or price * 0.01) * 0.75)
    threshold = max(price * 0.008, (atr or price * 0.01) * 0.75)
    out: list[dict[str, Any]] = []

    def add(name_fa: str, name_en: str, direction: str, state: str, description: str,
            score: int, trigger: float | None = None, invalidation: float | None = None,
            potential_direction: str | None = None) -> None:
        out.append(_item(
            name_fa=name_fa, name_en=name_en, category="GEOMETRIC",
            direction=direction, state=state,
            strength="MODERATE" if state == "CONFIRMED" else "WEAK",
            description=description, timestamp=latest_time, score=score,
            trigger_price=trigger, invalidation_price=invalidation,
            potential_direction=potential_direction,
        ))

    # Three-point double top / bottom.
    last3 = pivots[-3:]
    kinds3 = [p.get("kind") for p in last3]
    if kinds3 == ["HIGH", "LOW", "HIGH"]:
        first, neck, second = last3
        tops_close = abs(float(first["price"]) - float(second["price"])) <= tolerance
        neckline = float(neck["price"])
        if tops_close:
            confirmed = latest_close < neckline
            add("سقف دوقلو", "Double Top", "BEARISH" if confirmed else "NEUTRAL",
                "CONFIRMED" if confirmed else "FORMING",
                "دو قله در محدودهٔ قیمتی نزدیک؛ شکست و بسته‌شدن زیر خط گردن شرط تأیید است.",
                -18 if confirmed else 0, trigger=neckline,
                invalidation=max(float(first["price"]), float(second["price"])),
                potential_direction=None if confirmed else "BEARISH")
    elif kinds3 == ["LOW", "HIGH", "LOW"]:
        first, neck, second = last3
        bottoms_close = abs(float(first["price"]) - float(second["price"])) <= tolerance
        neckline = float(neck["price"])
        if bottoms_close:
            confirmed = latest_close > neckline
            add("کف دوقلو", "Double Bottom", "BULLISH" if confirmed else "NEUTRAL",
                "CONFIRMED" if confirmed else "FORMING",
                "دو کف در محدودهٔ قیمتی نزدیک؛ شکست و بسته‌شدن بالای خط گردن شرط تأیید است.",
                18 if confirmed else 0, trigger=neckline,
                invalidation=min(float(first["price"]), float(second["price"])),
                potential_direction=None if confirmed else "BULLISH")

    # Head-and-shoulders and inverse head-and-shoulders.
    last5 = pivots[-5:]
    kinds5 = [p.get("kind") for p in last5]
    if kinds5 == ["HIGH", "LOW", "HIGH", "LOW", "HIGH"]:
        left, low1, head, low2, right = last5
        shoulders_match = abs(float(left["price"]) - float(right["price"])) <= tolerance
        head_higher = float(head["price"]) - max(float(left["price"]), float(right["price"])) >= threshold
        if shoulders_match and head_higher:
            neckline = (float(low1["price"]) + float(low2["price"])) / 2.0
            confirmed = latest_close < neckline
            add("سر و شانه", "Head and Shoulders", "BEARISH" if confirmed else "NEUTRAL",
                "CONFIRMED" if confirmed else "FORMING",
                "شانه‌ها تقریباً هم‌سطح و سر بالاتر است؛ بسته‌شدن زیر خط گردن الگو را تأیید می‌کند.",
                -22 if confirmed else 0, trigger=neckline,
                invalidation=float(head["price"]),
                potential_direction=None if confirmed else "BEARISH")
    elif kinds5 == ["LOW", "HIGH", "LOW", "HIGH", "LOW"]:
        left, high1, head, high2, right = last5
        shoulders_match = abs(float(left["price"]) - float(right["price"])) <= tolerance
        head_lower = min(float(left["price"]), float(right["price"])) - float(head["price"]) >= threshold
        if shoulders_match and head_lower:
            neckline = (float(high1["price"]) + float(high2["price"])) / 2.0
            confirmed = latest_close > neckline
            add("سر و شانهٔ معکوس", "Inverse Head and Shoulders", "BULLISH" if confirmed else "NEUTRAL",
                "CONFIRMED" if confirmed else "FORMING",
                "دو شانهٔ کف تقریباً هم‌سطح و سر پایین‌تر است؛ بسته‌شدن بالای خط گردن شرط تأیید است.",
                22 if confirmed else 0, trigger=neckline,
                invalidation=float(head["price"]),
                potential_direction=None if confirmed else "BULLISH")

    # Triangles, wedges and channels are fitted to confirmed alternating swing points.
    recent = pivots[-8:]
    highs = [p for p in recent if p.get("kind") == "HIGH"]
    lows = [p for p in recent if p.get("kind") == "LOW"]
    if len(highs) >= 2 and len(lows) >= 2:
        hx = np.asarray([float(p["index"]) for p in highs], dtype=float)
        hy = np.asarray([float(p["price"]) for p in highs], dtype=float)
        lx = np.asarray([float(p["index"]) for p in lows], dtype=float)
        ly = np.asarray([float(p["price"]) for p in lows], dtype=float)
        hs = float(np.polyfit(hx, hy, 1)[0])
        ls = float(np.polyfit(lx, ly, 1)[0])
        scale = max(price, float(np.mean(np.r_[hy, ly])), 1e-15)
        hs_pct, ls_pct = hs / scale, ls / scale
        eps = 0.00008
        common_start = max(float(np.min(hx)), float(np.min(lx)))
        common_end = min(float(np.max(hx)), float(np.max(lx)))
        narrowing = False
        if common_end > common_start:
            h_line = np.polyfit(hx, hy, 1)
            l_line = np.polyfit(lx, ly, 1)
            width_start = float(np.polyval(h_line, common_start) - np.polyval(l_line, common_start))
            width_end = float(np.polyval(h_line, common_end) - np.polyval(l_line, common_end))
            narrowing = width_start > 0 and width_end > 0 and width_end <= width_start * 0.80
        if narrowing and hs_pct < -eps and ls_pct > eps:
            add("مثلث متقارن", "Symmetrical Triangle", "NEUTRAL", "FORMING",
                "مقاومت نزولی و حمایت صعودی به هم نزدیک می‌شوند؛ جهت شکست هنوز معلوم نیست.", 0)
        elif narrowing and abs(hs_pct) <= eps and ls_pct > eps:
            add("مثلث صعودی", "Ascending Triangle", "BULLISH", "FORMING",
                "مقاومت تقریباً افقی و کف‌های بالاتر؛ شکست معتبر بالای مقاومت لازم است.", 7,
                trigger=float(np.polyval(np.polyfit(hx, hy, 1), common_end)))
        elif narrowing and hs_pct < -eps and abs(ls_pct) <= eps:
            add("مثلث نزولی", "Descending Triangle", "BEARISH", "FORMING",
                "سقف‌های پایین‌تر و حمایت تقریباً افقی؛ شکست معتبر زیر حمایت لازم است.", -7,
                trigger=float(np.polyval(np.polyfit(lx, ly, 1), common_end)))
        elif hs_pct > eps and ls_pct > hs_pct + eps and narrowing:
            add("کنج صعودی", "Rising Wedge", "BEARISH", "FORMING",
                "هر دو خط روند صعودی‌اند اما حمایت سریع‌تر بالا می‌آید؛ شکست حمایت می‌تواند الگو را تأیید کند.", -8)
        elif hs_pct < -eps and ls_pct < hs_pct - eps and narrowing:
            add("کنج نزولی", "Falling Wedge", "BULLISH", "FORMING",
                "هر دو خط روند نزولی‌اند اما سقف‌ها آهسته‌تر پایین می‌آیند؛ شکست مقاومت می‌تواند الگو را تأیید کند.", 8)
        else:
            parallel = abs(hs_pct - ls_pct) <= max(abs(hs_pct), abs(ls_pct), eps) * 0.35
            if parallel and hs_pct > eps and ls_pct > eps:
                add("کانال صعودی", "Ascending Channel", "BULLISH", "FORMING",
                    "خطوط سقف و کف تقریباً موازی و صعودی‌اند؛ شکست کف کانال ریسک تغییر روند دارد.", 6)
            elif parallel and hs_pct < -eps and ls_pct < -eps:
                add("کانال نزولی", "Descending Channel", "BEARISH", "FORMING",
                    "خطوط سقف و کف تقریباً موازی و نزولی‌اند؛ شکست سقف کانال ریسک تغییر روند دارد.", -6)

    # Keep the dashboard concise and avoid duplicate family labels.
    unique: dict[str, dict[str, Any]] = {}
    for pattern in out:
        unique[pattern["name_en"]] = pattern
    return list(unique.values())[-5:]


def analyze_market_patterns(
    candles: pd.DataFrame,
    pivots: list[dict[str, Any]],
    elliott_waves: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Analyze candles, classic formations, geometry, waves, trend and momentum."""
    if not isinstance(candles, pd.DataFrame) or len(candles) < 30:
        return {
            "status": "INSUFFICIENT_DATA",
            "direction": {"bias": "UNKNOWN", "label_fa": "داده ناکافی", "score": None},
            "candlestick_patterns": [],
            "geometric_patterns": [],
            "wave_structure": {"pattern": "UNKNOWN", "phase": "INSUFFICIENT_DATA"},
            "reason": "حداقل ۳۰ کندل بسته‌شده برای تحلیل چندگانه لازم است.",
        }

    close = pd.to_numeric(candles["close"], errors="coerce")
    high = pd.to_numeric(candles["high"], errors="coerce")
    low = pd.to_numeric(candles["low"], errors="coerce")
    volume = pd.to_numeric(candles["volume"], errors="coerce")
    if (close.isna() | high.isna() | low.isna() | volume.isna()).any():
        raise ValueError("market pattern analysis requires valid OHLCV data")

    ema20 = close.ewm(span=20, adjust=False, min_periods=20).mean()
    ema50 = close.ewm(span=50, adjust=False, min_periods=50).mean()
    ema200 = close.ewm(span=200, adjust=False, min_periods=200).mean()
    last_close = float(close.iloc[-1])
    e20 = float(ema20.iloc[-1]) if _finite(ema20.iloc[-1]) else None
    e50 = float(ema50.iloc[-1]) if _finite(ema50.iloc[-1]) else None
    e200 = float(ema200.iloc[-1]) if _finite(ema200.iloc[-1]) else None
    rsi = _rsi(close)
    adx14 = _adx(candles)
    divergence = _divergence(candles, pivots or [])
    fibonacci = _fibonacci_setup(pivots or [])
    macd_line = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    macd_signal = macd_line.ewm(span=9, adjust=False).mean()
    macd_hist = float((macd_line - macd_signal).iloc[-1])
    atr = _atr(candles)
    atr_pct = atr / last_close * 100.0 if atr is not None and last_close > 0 else None
    volume_base = float(volume.tail(21).iloc[:-1].median()) if len(volume.tail(21)) > 1 else 0.0
    volume_ratio = float(volume.iloc[-1] / volume_base) if volume_base > 0 else None
    change_10 = float(close.iloc[-1] / close.iloc[-11] - 1.0) * 100.0 if len(close) >= 11 else 0.0

    trend = "NEUTRAL"
    if e50 is not None and e200 is not None:
        if last_close > e50 > e200:
            trend = "BULLISH"
        elif last_close < e50 < e200:
            trend = "BEARISH"
    elif e20 is not None and e50 is not None:
        if last_close > e20 and e20 > e50:
            trend = "BULLISH"
        elif last_close < e20 and e20 < e50:
            trend = "BEARISH"

    candle_patterns = _candle_patterns(candles, volume_ratio)
    geometric_patterns = _geometric_patterns(candles, pivots or [], atr)
    wave = elliott_waves or {}
    wave_pattern = str(wave.get("pattern") or "UNKNOWN")
    wave_state = str(wave.get("status") or "UNKNOWN")

    score = 0
    trend_weight = 25 if adx14 is None or adx14 >= 20 else 0
    score += trend_weight if trend == "BULLISH" else -trend_weight if trend == "BEARISH" else 0
    if divergence.get("type") == "BEARISH":
        score -= 10
    elif divergence.get("type") == "BULLISH":
        score += 10
    score += 12 if macd_hist > 0 else -12 if macd_hist < 0 else 0
    score += 12 if rsi is not None and rsi >= 55 else -12 if rsi is not None and rsi <= 45 else 0
    score += 10 if change_10 > 0.6 else -10 if change_10 < -0.6 else 0
    if volume_ratio is not None and volume_ratio >= 1.20:
        score += 5 if change_10 > 0 else -5 if change_10 < 0 else 0
    for pattern in candle_patterns:
        if pattern["direction"] in {"BULLISH", "BEARISH"}:
            score += int(pattern["score"] * (1.0 if pattern["state"] == "CONFIRMED" else 0.50))
    for pattern in geometric_patterns:
        if pattern["state"] == "CONFIRMED" and pattern["direction"] in {"BULLISH", "BEARISH"}:
            score += int(pattern["score"])
        elif pattern["direction"] in {"BULLISH", "BEARISH"}:
            score += int(pattern["score"] * 0.35)
    if wave_pattern == "BULLISH" and wave_state in {"CANDIDATE", "CONFIRMED"}:
        score += 8
    elif wave_pattern == "BEARISH" and wave_state in {"CANDIDATE", "CONFIRMED"}:
        score -= 8
    score = max(-100, min(100, int(score)))

    if score >= 25:
        bias, label = "BULLISH", "تمایل صعودی"
    elif score <= -25:
        bias, label = "BEARISH", "تمایل نزولی"
    else:
        bias, label = "NEUTRAL", "خنثی / بدون برتری روشن"

    rsi_state = "UNKNOWN" if rsi is None else "OVERSOLD" if rsi <= 30 else "OVERBOUGHT" if rsi >= 70 else "NEUTRAL"
    if adx14 is not None and adx14 < 20:
        regime = "RANGING"
    elif adx14 is not None and adx14 > 25:
        regime = "TRENDING"
    else:
        regime = "TRANSITION"
    wave_structure = {
        "pattern": wave_pattern,
        "status": wave_state,
        "phase": wave.get("phase", "UNKNOWN"),
        "wave_count": wave.get("wave_count"),
        "next_wave": wave.get("next_wave"),
        "invalidation_price": wave.get("invalidation_price"),
        "reason": wave.get("reason", "شواهد کافی برای شمارش موج وجود ندارد."),
        "confidence_semantics": wave.get(
            "confidence_semantics", "برچسب موج فرضیهٔ ساختاری است، نه احتمال."
        ),
    }
    bullish_count = sum(1 for p in candle_patterns + geometric_patterns if p["direction"] == "BULLISH")
    bearish_count = sum(1 for p in candle_patterns + geometric_patterns if p["direction"] == "BEARISH")
    return {
        "status": "PASS" if e50 is not None else "PARTIAL",
        "direction": {
            "bias": bias,
            "adx14": round(adx14, 2) if adx14 is not None else None,
            "ema200": round(e200, 12) if e200 is not None else None,
            "divergence": divergence,
            "fibonacci": fibonacci,
            "label_fa": label,
            "score": score,
            "score_semantics": "امتیاز همگرایی شواهد از ۱۰۰- تا ۱۰۰+ است؛ احتمال آماری موفقیت نیست.",
            "trend": trend,
            "regime": regime,
            "price_change_10_candles_pct": round(change_10, 3),
            "rsi14": round(rsi, 2) if rsi is not None else None,
            "rsi_state": rsi_state,
            "macd_histogram": round(macd_hist, 12) if _finite(macd_hist) else None,
            "ema20": round(e20, 12) if e20 is not None else None,
            "ema50": round(e50, 12) if e50 is not None else None,
            "atr_pct": round(atr_pct, 3) if atr_pct is not None else None,
            "volume_ratio": round(volume_ratio, 3) if volume_ratio is not None else None,
            "bullish_pattern_count": bullish_count,
            "bearish_pattern_count": bearish_count,
        },
        "candlestick_patterns": candle_patterns,
        "geometric_patterns": geometric_patterns,
        "wave_structure": wave_structure,
        "summary": [
            "فقط کندل‌های بسته‌شده در این تحلیل استفاده شده‌اند.",
            "الگوهای در حال تشکیل تا زمان شکست/تأیید، سیگنال قطعی محسوب نمی‌شوند.",
            "امتیاز جهت بازار احتمال موفقیت یا تضمین حرکت آینده نیست.",
        ],
        "reason": None,
    }
