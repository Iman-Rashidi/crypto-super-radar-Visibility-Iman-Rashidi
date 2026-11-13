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


def _candle_patterns(frame: pd.DataFrame, volume_ratio: float | None) -> list[dict[str, Any]]:
    if len(frame) < 3:
        return []
    bars = frame.tail(3)
    first, middle, last = bars.iloc[-3], bars.iloc[-2], bars.iloc[-1]
    o, h, low, c = (float(last[k]) for k in ("open", "high", "low", "close"))
    fo, fh, fl, fc = (float(first[k]) for k in ("open", "high", "low", "close"))
    po, ph, pl, pc = (float(middle[k]) for k in ("open", "high", "low", "close"))
    mo, mh, ml, mc = fo, fh, fl, fc
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

    if first_bear and first_body >= max((ph - pl) * 0.45, 1e-15) and middle_body <= max((mh - ml) * 0.30, 1e-15) and last_bull and c > first_mid and c > mc:
        add("ستاره صبحگاهی", "Morning Star", "BULLISH",
            "سه کندلِ فشار فروش، مکث و بازپس‌گیری بیش از نیمهٔ بدنهٔ کندل اول؛ تأیید با بسته‌شدن بعدی بالای سقف الگو.", 15)
    elif first_bull and first_body >= max((ph - pl) * 0.45, 1e-15) and middle_body <= max((mh - ml) * 0.30, 1e-15) and last_bear and c < first_mid and c < mc:
        add("ستاره شامگاهی", "Evening Star", "BEARISH",
            "سه کندلِ فشار خرید، مکث و پس‌گرفتن بیش از نیمهٔ بدنهٔ کندل اول؛ تأیید با بسته‌شدن بعدی زیر کف الگو.", -15)

    if pc < po and last_bull and o <= pc and c >= po and body >= abs(pc - po):
        add("پوشای صعودی", "Bullish Engulfing", "BULLISH",
            "بدنهٔ صعودی آخر، بدنهٔ نزولی قبلی را پوشانده است؛ در روند نزولی معنا‌دارتر است.", 13)
    elif pc > po and last_bear and o >= pc and c <= po and body >= abs(pc - po):
        add("پوشای نزولی", "Bearish Engulfing", "BEARISH",
            "بدنهٔ نزولی آخر، بدنهٔ صعودی قبلی را پوشانده است؛ در روند صعودی معنا‌دارتر است.", -13)

    if pc < po and last_bull and o < pl and c > (po + pc) / 2.0 and c < po:
        add("خط نفوذی صعودی", "Piercing Line", "BULLISH",
            "کندل صعودی بیش از نیمهٔ بدنهٔ نزولی قبلی را بازیابی کرده است.", 10)
    elif pc > po and last_bear and o > ph and c < (po + pc) / 2.0 and c > po:
        add("ابر تیره", "Dark Cloud Cover", "BEARISH",
            "کندل نزولی بیش از نیمهٔ بدنهٔ صعودی قبلی را پس گرفته است.", -10)

    # The classic three-soldiers / three-crows formations.
    three = frame.tail(3)
    bodies = (three["close"] - three["open"]).to_numpy(dtype=float)
    ranges = (three["high"] - three["low"]).to_numpy(dtype=float)
    if np.all(bodies > 0) and np.all(bodies >= np.maximum(ranges * 0.45, 1e-15)):
        if np.all(np.diff(three["close"].to_numpy(dtype=float)) > 0):
            add("سه سرباز سفید", "Three White Soldiers", "BULLISH",
                "سه کندل صعودی با بدنه‌های نسبتاً قوی و بسته‌شدن‌های پیاپی بالاتر.", 14)
    elif np.all(bodies < 0) and np.all(-bodies >= np.maximum(ranges * 0.45, 1e-15)):
        if np.all(np.diff(three["close"].to_numpy(dtype=float)) < 0):
            add("سه کلاغ سیاه", "Three Black Crows", "BEARISH",
                "سه کندل نزولی با بدنه‌های نسبتاً قوی و بسته‌شدن‌های پیاپی پایین‌تر.", -14)

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
            add("مرد آویزان", "Hanging Man", "BEARISH",
                "شکل چکشی پس از رشد اخیر؛ فقط با شکست کف الگو نشانهٔ برگشت نزولی می‌شود.", -9)
        else:
            add("چکش‌مانند", "Hammer-like", "NEUTRAL",
                "شکل چکشی دیده شده اما روند پیشین برای تفسیر جهت‌دار روشن نیست.", 0)
    elif small_body and long_upper and short_lower and close_location <= 0.40:
        if context == "DOWN":
            add("چکش معکوس", "Inverted Hammer", "BULLISH",
                "سایهٔ بالایی بلند پس از افت؛ بازگشت فقط با تأیید کندل بعدی معتبرتر می‌شود.", 9)
        elif context == "UP":
            add("ستاره ثاقب", "Shooting Star", "BEARISH",
                "سایهٔ بالایی بلند پس از رشد؛ شکست کف الگو برای تأیید لازم است.", -11)
        else:
            add("ستاره معکوس خنثی", "Inverted Star", "NEUTRAL",
                "سایهٔ بالایی بلند بدون زمینهٔ روند مشخص؛ جهت برگشت هنوز معلوم نیست.", 0)

    if body_ratio <= 0.10:
        add("دوجی", "Doji", "NEUTRAL",
            "بدنه بسیار کوچک نسبت به دامنهٔ کندل؛ نشان‌دهندهٔ تردید است، نه سیگنال مستقل خرید یا فروش.", 0, strength="MODERATE")
    elif body_ratio <= 0.30 and upper >= candle_range * 0.20 and lower >= candle_range * 0.20:
        add("اسپینینگ‌تاپ", "Spinning Top", "NEUTRAL",
            "بدنه کوچک و دو سایهٔ قابل‌توجه؛ تعادل موقت خریدار و فروشنده.", 0, strength="WEAK")

    if h < ph and low > pl:
        add("کندل داخلی", "Inside Bar", "NEUTRAL",
            "دامنهٔ کندل آخر داخل دامنهٔ کندل قبلی است؛ شکست هر طرف می‌تواند جهت حرکت بعدی را روشن کند.", 0, strength="WEAK")

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
            evidence_strength="MODERATE" if state == "CONFIRMED" else "WEAK",
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
    last_close = float(close.iloc[-1])
    e20 = float(ema20.iloc[-1]) if _finite(ema20.iloc[-1]) else None
    e50 = float(ema50.iloc[-1]) if _finite(ema50.iloc[-1]) else None
    rsi = _rsi(close)
    macd_line = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    macd_signal = macd_line.ewm(span=9, adjust=False).mean()
    macd_hist = float((macd_line - macd_signal).iloc[-1])
    atr = _atr(candles)
    atr_pct = atr / last_close * 100.0 if atr is not None and last_close > 0 else None
    volume_base = float(volume.tail(21).iloc[:-1].median()) if len(volume.tail(21)) > 1 else 0.0
    volume_ratio = float(volume.iloc[-1] / volume_base) if volume_base > 0 else None
    change_10 = float(close.iloc[-1] / close.iloc[-11] - 1.0) * 100.0 if len(close) >= 11 else 0.0

    trend = "NEUTRAL"
    if e20 is not None and e50 is not None:
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
    score += 25 if trend == "BULLISH" else -25 if trend == "BEARISH" else 0
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
    if trend == "NEUTRAL":
        regime = "RANGE_OR_TRANSITION"
    else:
        regime = "TRENDING"
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
