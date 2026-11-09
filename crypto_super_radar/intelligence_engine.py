"""Unified evidence combiner for whale, sentiment, market and derivatives modules.

This module is intentionally fail-closed. It returns research-only labels and
never submits orders or treats a score as a probability of profit.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional


def combine_intelligence(
    *,
    whale: Mapping[str, Any],
    sentiment: Mapping[str, Any],
    market: Mapping[str, Any],
    derivatives: Mapping[str, Any],
    security_status: str,
    backtest_status: str,
    score_threshold: float = 70.0,
) -> dict[str, Any]:
    """Combine independent evidence blocks into LONG/SHORT/NO TRADE research output."""
    gate_states = {
        "whale": "PASS" if whale.get("status") == "KNOWN" and (
            whale.get("net_exchange_flow_usd") is not None or
            whale.get("net_whale_transfer_usd") is not None
        ) else "UNKNOWN",
        "sentiment": "PASS" if sentiment.get("status") == "KNOWN" else "UNKNOWN",
        "market": str(market.get("status", "UNKNOWN")).upper(),
        "derivatives": str(derivatives.get("status", "UNKNOWN")).upper(),
        "security": str(security_status).upper(),
        "walk_forward": str(backtest_status).upper(),
    }
    for key, state in list(gate_states.items()):
        if state not in {"PASS", "FAIL", "UNKNOWN"}:
            gate_states[key] = "UNKNOWN"
    blocked = [key for key, state in gate_states.items() if state != "PASS"]
    sentiment_score = sentiment.get("score")
    exchange_net = whale.get("net_exchange_flow_usd")
    wallet_net = whale.get("net_whale_transfer_usd")
    whale_bias = "UNKNOWN"
    bias_points = []
    if exchange_net is not None:
        bias_points.append(-1 if exchange_net > 0 else 1 if exchange_net < 0 else 0)
    if wallet_net is not None:
        bias_points.append(1 if wallet_net > 0 else -1 if wallet_net < 0 else 0)
    bias_total = sum(bias_points)
    if bias_points:
        whale_bias = "ACCUMULATION_PRESSURE" if bias_total > 0 else (
            "DISTRIBUTION_PRESSURE" if bias_total < 0 else "MIXED_OR_BALANCED"
        )
    market_direction = market.get("direction")
    derivatives_direction = derivatives.get("direction")
    divergence = None
    if sentiment_score is not None and whale_bias != "UNKNOWN":
        if sentiment_score >= 25 and whale_bias == "DISTRIBUTION_PRESSURE":
            divergence = "POSITIVE_SENTIMENT_WITH_WHALE_DISTRIBUTION_PRESSURE"
        elif sentiment_score <= -25 and whale_bias == "ACCUMULATION_PRESSURE":
            divergence = "NEGATIVE_SENTIMENT_WITH_WHALE_ACCUMULATION_PRESSURE"
    # Direction is corroborative only; never convert exchange flow into a buy/sell.
    corroboration = 0
    corroboration += int(market_direction in {"LONG", "SHORT"})
    corroboration += int(derivatives_direction in {"LONG", "SHORT"})
    corroboration += int(sentiment_score is not None)
    corroboration += int(whale_bias != "UNKNOWN")
    confirmation_score: Optional[float] = None
    if not blocked:
        # This score describes evidence agreement, not a probability.
        agreement = 0
        if market_direction == derivatives_direction and market_direction in {"LONG", "SHORT"}:
            agreement += 35
        if sentiment_score is not None:
            agreement += 15 if (sentiment_score >= 25 and market_direction == "LONG") or (
                sentiment_score <= -25 and market_direction == "SHORT") else 5
        if whale_bias != "UNKNOWN":
            if (whale_bias == "ACCUMULATION_PRESSURE" and market_direction == "LONG") or (
                whale_bias == "DISTRIBUTION_PRESSURE" and market_direction == "SHORT"):
                agreement += 20
            elif whale_bias == "MIXED_OR_BALANCED":
                agreement += 0
            else:
                agreement += 5
        confirmation_score = min(100.0, float(agreement))
    signal = "NO TRADE"
    if not blocked and confirmation_score is not None and confirmation_score >= score_threshold:
        if market_direction == derivatives_direction and market_direction in {"LONG", "SHORT"}:
            signal = market_direction
    confidence_score = 100.0 * sum(state == "PASS" for state in gate_states.values()) / len(gate_states)
    estimated_risk = market.get("estimated_risk_score")
    opportunity_rank = market.get("opportunity_rank")
    return {
        "signal": signal,
        "estimated_risk_score": estimated_risk if isinstance(estimated_risk, (int, float)) else None,
        "opportunity_rank": opportunity_rank if isinstance(opportunity_rank, (int, float)) else None,
        "confidence_score": confidence_score,
        "confidence_semantics": "Mandatory evidence coverage score; not a probability of profit.",
        "gate_states": gate_states,
        "blocked_by": blocked,
        "confirmation_score": confirmation_score,
        "score_semantics": "Evidence-agreement score; not a probability of profit.",
        "whale_sentiment_divergence": divergence,\n        "whale_bias": whale_bias,\n        "whale_exchange_net_flow_usd": exchange_net,\n        "whale_wallet_net_transfer_usd": wallet_net,
        "evidence_coverage_count": corroboration,
        "decision_policy": "Any FAIL or UNKNOWN in a mandatory gate blocks the signal.",
        "execution": "PAPER_RESEARCH_ONLY",
    }
