"""Evidence-weighted, research-only future forecast helpers.

No forecast produced here is an order instruction. Missing evidence remains unknown.
"""
from __future__ import annotations

import math
from typing import Any


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _levels(candles: Any) -> dict[str, float | None]:
    if not isinstance(candles, list) or not candles:
        return {"support": None, "resistance": None}
    highs = [_finite(row.get("high")) for row in candles if isinstance(row, dict)]
    lows = [_finite(row.get("low")) for row in candles if isinstance(row, dict)]
    highs = [v for v in highs if v is not None]
    lows = [v for v in lows if v is not None]
    return {
        "support": min(lows[-48:]) if lows else None,
        "resistance": max(highs[-48:]) if highs else None,
    }


def build_future_forecast(
    candles: list[dict[str, Any]],
    *,
    symbol: str,
    interval: str,
    wave: dict[str, Any] | None = None,
    divergence: dict[str, Any] | None = None,
    market_flow: dict[str, Any] | None = None,
    higher_timeframes: dict[str, list[dict[str, Any]]] | None = None,
    news: list[dict[str, Any]] | None = None,
    horizon_steps: int = 12,
) -> dict[str, Any]:
    """Combine only supplied, observable evidence into one scenario and chart overlays."""
    bars = [row for row in candles if isinstance(row, dict) and _finite(row.get("close")) is not None]
    if len(bars) < 15:
        return {
            "status": "INSUFFICIENT_DATA", "scenario": "NEUTRAL", "confidence_pct": None,
            "symbol": symbol, "whale_summary_fa": "داده تأییدشده موجود نیست",
            "news_summary_fa": "داده تأییدشده موجود نیست", "confidence_band": [],
            "bullish_start": None, "stagnation_start": None, "invalidation_price": None,
            "higher_timeframes": {}, "evidence": [],
        }
    closes = [_finite(row.get("close")) for row in bars]
    highs = [_finite(row.get("high")) for row in bars]
    lows = [_finite(row.get("low")) for row in bars]
    last = closes[-1]
    true_ranges = []
    for i, row in enumerate(bars):
        hi, lo = highs[i], lows[i]
        prev = closes[i - 1] if i else closes[i]
        if hi is not None and lo is not None:
            true_ranges.append(max(hi - lo, abs(hi - prev), abs(lo - prev)))
    atr = sum(true_ranges[-14:]) / min(14, len(true_ranges)) if true_ranges else 0.0
    atr = atr if math.isfinite(atr) and atr > 0 else max(abs(last) * 0.001, 1e-12)

    evidence: list[dict[str, Any]] = []
    score = 0.0
    coverage = 0.0
    wave = wave if isinstance(wave, dict) else {}
    wave_pattern = str(wave.get("pattern") or "").upper()
    if wave_pattern in {"BULLISH", "BEARISH"} and int(wave.get("wave_count") or 0) >= 2:
        value = 2.0 if wave_pattern == "BULLISH" else -2.0
        score += value; coverage += 2.0
        evidence.append({"source": "elliott_wave", "direction": wave_pattern, "weight": value})
    divergence = divergence if isinstance(divergence, dict) else {}
    div_type = str(divergence.get("type") or "").upper()
    if div_type in {"BULLISH", "BEARISH"} and divergence.get("confirmed", True):
        value = 2.5 if div_type == "BULLISH" else -2.5
        score += value; coverage += 2.5
        evidence.append({"source": "rsi_divergence", "direction": div_type, "weight": value})

    flow = market_flow if isinstance(market_flow, dict) else {}
    whale = flow.get("whale_flow") if isinstance(flow.get("whale_flow"), dict) else {}
    large = flow.get("large_trades") if isinstance(flow.get("large_trades"), list) else []
    imbalance = _finite((flow.get("order_book") or {}).get("imbalance_pct")) if isinstance(flow.get("order_book"), dict) else None
    whale_bias = str(whale.get("bias") or "").upper()
    whale_verified = bool(large) or imbalance is not None
    if whale_verified and whale_bias in {"BULLISH_FLOW", "BEARISH_FLOW"}:
        value = 2.0 if whale_bias == "BULLISH_FLOW" else -2.0
        score += value; coverage += 2.0
        evidence.append({"source": "whale_flow", "direction": whale_bias, "weight": value})
        whale_summary = f"{whale.get('label') or whale_bias} · معاملات بزرگ ثبت‌شده: {len(large)} · عدم‌توازن دفتر سفارش: {imbalance if imbalance is not None else 'ناموجود'}"
    else:
        whale_summary = "داده تأییدشده موجود نیست"
    if not whale_verified:
        evidence.append({"source": "whale_flow", "direction": "UNKNOWN", "weight": 0.0})

    news_items = [item for item in (news or []) if isinstance(item, dict) and str(item.get("title") or "").strip()]
    if news_items:
        news_score = 0.0
        positive_terms = {"surge", "rally", "approval", "approved", "adoption", "inflow", "bullish", "breakout", "record high", "partnership", "upgrade"}
        negative_terms = {"crash", "hack", "exploit", "lawsuit", "ban", "outflow", "bearish", "liquidation", "probe", "insolvency", "delist"}
        for item in news_items[:8]:
            sentiment = str(item.get("sentiment") or "").upper()
            if sentiment in {"POSITIVE", "BULLISH"}:
                news_score += 0.25
            elif sentiment in {"NEGATIVE", "BEARISH"}:
                news_score -= 0.25
            elif not sentiment:
                headline_text = str(item.get("title") or "").lower()
                news_score += 0.15 * sum(term in headline_text for term in positive_terms)
                news_score -= 0.15 * sum(term in headline_text for term in negative_terms)
        news_score = max(-1.5, min(1.5, news_score))
        if news_score:
            score += news_score; coverage += 1.5
        headline = str(news_items[0].get("title") or "")[:220]
        source = str(news_items[0].get("source") or "منبع عمومی خبر")
        news_summary = f"{headline} · {source}"
        evidence.append({"source": "asset_news", "sentiment_method": "provider_label_or_transparent_headline_keyword_heuristic", "direction": "BULLISH" if news_score > 0 else "BEARISH" if news_score < 0 else "NEUTRAL", "weight": round(news_score, 3)})
    else:
        news_summary = "داده تأییدشده موجود نیست"
        evidence.append({"source": "asset_news", "direction": "UNKNOWN", "weight": 0.0})

    htf = {}
    htf_data = higher_timeframes if isinstance(higher_timeframes, dict) else {}
    for timeframe in ("1h", "4h"):
        levels = _levels(htf_data.get(timeframe))
        htf[timeframe] = levels
    nearest_support = min((v for v in (htf["1h"]["support"], htf["4h"]["support"]) if v is not None), default=None)
    nearest_resistance = min((v for v in (htf["1h"]["resistance"], htf["4h"]["resistance"]) if v is not None and v >= last), default=None)
    if any(v is not None for levels in htf.values() for v in levels.values()):
        coverage += 1.5
        if nearest_support is not None and last <= nearest_support + atr:
            score += 0.75
        if nearest_resistance is not None and nearest_resistance - last <= atr:
            score -= 0.75
        evidence.append({"source": "higher_timeframe_levels", "direction": "SUPPORT_RESISTANCE_CONTEXT", "weight": round(score, 3)})
    else:
        evidence.append({"source": "higher_timeframe_levels", "direction": "UNKNOWN", "weight": 0.0})

    if score >= 1.25:
        scenario = "BULLISH"
    elif score <= -1.25:
        scenario = "BEARISH"
    else:
        scenario = "NEUTRAL"
    confidence = round(min(88.0, 50.0 + (abs(score) / max(coverage, 1.0)) * 38.0), 1) if coverage else None
    direction = 1.0 if scenario == "BULLISH" else -1.0 if scenario == "BEARISH" else 0.0
    steps = max(3, min(int(horizon_steps), 32))
    band = []
    drift = direction * min(atr * 0.22, abs(last) * 0.004)
    for step in range(1, steps + 1):
        center = last + drift * step
        radius = atr * (0.65 + 0.18 * math.sqrt(step))
        band.append({"step": step, "center": round(center, 12), "lower": round(center - radius, 12), "upper": round(center + radius, 12)})

    local_high = max(v for v in highs[-20:] if v is not None)
    local_low = min(v for v in lows[-20:] if v is not None)
    bullish_start = round(max(local_high, nearest_resistance or local_high) + 0.15 * atr, 12) if scenario == "BULLISH" else None
    stagnation = nearest_resistance if nearest_resistance is not None else local_high
    stagnation_start = {"lower": round(stagnation - 0.25 * atr, 12), "upper": round(stagnation + 0.25 * atr, 12)} if scenario != "NEUTRAL" else None
    invalidation = (nearest_support if nearest_support is not None else local_low) - 0.35 * atr if scenario == "BULLISH" else (nearest_resistance if nearest_resistance is not None else local_high) + 0.35 * atr if scenario == "BEARISH" else None
    return {
        "status": "PASS" if scenario != "NEUTRAL" else "MIXED_EVIDENCE",
        "symbol": symbol, "interval": interval, "scenario": scenario,
        "confidence_pct": confidence, "score": round(score, 3), "atr": round(atr, 12),
        "confidence_band": band, "bullish_start": bullish_start,
        "stagnation_start": stagnation_start,
        "invalidation_price": round(invalidation, 12) if invalidation is not None else None,
        "higher_timeframes": htf, "whale_summary_fa": whale_summary,
        "news_summary_fa": news_summary, "evidence": evidence,
        "disclaimer_fa": "سناریوی پژوهشی احتمالی است؛ تضمین قیمت یا مجوز معامله نیست.",
    }
