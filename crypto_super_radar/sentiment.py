"""Transparent, offline market-sentiment scoring.

This is a baseline keyword model, not a language model or a live social crawler.
Scores are not probabilities. Missing source coverage is UNKNOWN, not neutral.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Iterable, Mapping, Optional

POSITIVE = {
    "adoption", "approved", "approval", "partnership", "integration", "inflow",
    "accumulation", "upgrade", "launch", "growth", "bullish", "breakout",
    "institutional", "record", "buyback", "صعود", "رشد", "تایید", "پذیرش", "انباشت", "خرید", "مثبت", "افزایش", "شکست", "مشارکت", "رونق",
}
NEGATIVE = {
    "hack", "exploit", "breach", "lawsuit", "ban", "delist", "outflow",
    "liquidation", "insolvency", "bankruptcy", "scam", "rug", "bearish",
    "blacklist", "depeg", "downtime", "drain", "نزول", "سقوط", "هک", "حمله", "کلاهبرداری", "ممنوعیت", "خروج", "فروش", "لیکویید", "ورشکستگی", "ریزش", "منفی", "کاهش", "سرقت",
}
INTENSIFIERS = {"massive", "surge", "record", "crash", "collapse", "extreme", "شدید", "عظیم"}


def score_text(text: str) -> dict:
    tokens = re.findall(r"[^\W\d_]+", (text or "").lower(), flags=re.UNICODE)
    if not tokens:
        return {"status": "UNKNOWN", "score": None, "positive_hits": 0,
                "negative_hits": 0, "intensity_hits": 0}
    positive = sum(token in POSITIVE for token in tokens)
    negative = sum(token in NEGATIVE for token in tokens)
    intensity = sum(token in INTENSIFIERS for token in tokens)
    denominator = positive + negative
    if denominator == 0:
        return {"status": "UNKNOWN", "score": None, "positive_hits": 0,
                "negative_hits": 0, "intensity_hits": intensity,
                "token_count": len(tokens)}
    score = 100.0 * (positive - negative) / denominator
    return {
        "status": "KNOWN", "score": score, "positive_hits": positive,
        "negative_hits": negative, "intensity_hits": intensity,
        "token_count": len(tokens),
    }


def analyze_market_sentiment(
    sources: Mapping[str, Iterable[str]],
    *,
    source_weights: Optional[Mapping[str, float]] = None,
    numeric_scores: Optional[Mapping[str, float]] = None,
) -> dict:
    """Combine explicitly supplied news/social/market-positioning text sources."""
    weights = dict(source_weights or {"news": 1.0, "social": 1.0, "positioning": 1.0})
    source_results = {}
    weighted_sum = 0.0
    total_weight = 0.0
    total_positive = total_negative = total_intensity = 0
    for source, items in sources.items():
        texts = [str(item) for item in items if str(item).strip()]
        if not texts:
            source_results[source] = {"status": "UNKNOWN", "score": None, "items": 0}
            continue
        scored = [score_text(item) for item in texts]
        valid = [s for s in scored if s["score"] is not None]
        if not valid:
            source_results[source] = {"status": "UNKNOWN", "score": None, "items": len(texts)}
            continue
        score = sum(s["score"] for s in valid) / len(valid)
        weight = float(weights.get(source, 1.0))
        if weight <= 0:
            raise ValueError("Source weights must be positive")
        weighted_sum += score * weight
        total_weight += weight
        total_positive += sum(s["positive_hits"] for s in valid)
        total_negative += sum(s["negative_hits"] for s in valid)
        total_intensity += sum(s["intensity_hits"] for s in valid)
        source_results[source] = {"status": "KNOWN", "score": score, "items": len(valid)}
    for source, raw_score in (numeric_scores or {}).items():
        try:
            numeric = float(raw_score)
        except (TypeError, ValueError):
            source_results[source] = {"status": "UNKNOWN", "score": None}
            continue
        if not -100.0 <= numeric <= 100.0:
            source_results[source] = {"status": "UNKNOWN", "score": None}
            continue
        weight = float(weights.get(source, 1.0))
        if weight <= 0:
            raise ValueError("Source weights must be positive")
        weighted_sum += numeric * weight
        total_weight += weight
        source_results[source] = {"status": "KNOWN", "score": numeric, "items": 1}
    if total_weight == 0:
        return {"status": "UNKNOWN", "score": None, "sources": source_results,
                "interpretation": "No usable sentiment observations were supplied."}
    score = weighted_sum / total_weight
    if score >= 25:
        label = "POSITIVE"
    elif score <= -25:
        label = "NEGATIVE"
    else:
        label = "MIXED_OR_NEUTRAL"
    return {
        "status": "KNOWN", "score": score, "label": label,
        "sources": source_results, "positive_hits": total_positive,
        "negative_hits": total_negative, "intensity_hits": total_intensity,
        "interpretation": "Keyword baseline only; source coverage and sampling bias limit inference.",
    }


def detect_sentiment_divergence(
    sentiment_score: Optional[float],
    whale_net_exchange_flow_usd: Optional[float],
    *,
    threshold_usd: float = 100_000.0,
) -> dict:
    """Flag disagreement; never infer a buy/sell from a transfer alone."""
    if sentiment_score is None or whale_net_exchange_flow_usd is None:
        return {"status": "UNKNOWN", "divergence": None,
                "reason": "Sentiment or classified exchange-flow evidence is missing."}
    if sentiment_score >= 25 and whale_net_exchange_flow_usd >= threshold_usd:
        return {"status": "KNOWN", "divergence": "BULLISH_SENTIMENT_WITH_EXCHANGE_INFLOW",
                "interpretation": "Positive sentiment conflicts with net exchange inflow; potential supply risk."}
    if sentiment_score <= -25 and whale_net_exchange_flow_usd <= -threshold_usd:
        return {"status": "KNOWN", "divergence": "BEARISH_SENTIMENT_WITH_EXCHANGE_OUTFLOW",
                "interpretation": "Negative sentiment conflicts with net exchange outflow; possible accumulation, not proof."}
    return {"status": "KNOWN", "divergence": "NO_STRONG_DIVERGENCE",
            "interpretation": "No configured sentiment/flow divergence detected."}



def fear_greed_to_sentiment(value: float) -> float:
    """Convert Alternative.me's 0..100 index to a centered -100..100 score."""
    number = float(value)
    if not 0.0 <= number <= 100.0:
        raise ValueError("Fear & Greed value must be between 0 and 100")
    return 2.0 * number - 100.0
