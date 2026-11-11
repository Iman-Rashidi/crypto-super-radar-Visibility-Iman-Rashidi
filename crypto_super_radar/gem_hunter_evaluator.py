"""Evidence-bounded first-stage evaluation for Gem Hunter discovery records.

This module evaluates only facts actually present in discovery payloads. It does
not infer holder distribution, contract safety, notional buy pressure, whale
swaps, or executable slippage from aggregate DEX pool statistics.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


FILTER_IDS = (
    "trend", "momentum", "relative_strength", "volume_confirmation",
    "buy_sell_pressure", "capital_flow", "whale_activity", "liquidity_depth",
    "market_integrity", "contract_security", "holder_distribution",
    "market_regime_validation",
)
CRITICAL_GATES = ("security", "liquidity", "data_quality", "exit")


def _number(value: Any):
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def evaluate_discovery_candidate(candidate: dict, *, now: datetime | None = None) -> dict:
    """Attach a conservative, explicit qualification record; never authorizes BUY."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)

    liquidity = _number(candidate.get("liquidity_usd"))
    volume = _number(candidate.get("volume_24h_usd"))
    ratio = _number(candidate.get("volume_to_liquidity_24h"))
    age = _number(candidate.get("pool_age_hours"))
    buys = _number(candidate.get("buys_24h"))
    sells = _number(candidate.get("sells_24h"))
    change = _number(candidate.get("price_change_1h_pct"))

    filters = {name: "UNKNOWN" for name in FILTER_IDS}
    reasons = {}

    # GeckoTerminal exposes transaction counts, not buy/sell notional volume.
    # Keep that distinction explicit: transaction share is not buy-volume share.
    if volume is not None and liquidity is not None and liquidity > 0:
        filters["volume_confirmation"] = (
            "PASS" if volume >= 100_000 and ratio is not None and 1 <= ratio <= 10
            else "FAIL"
        )
        reasons["volume_confirmation"] = "24h pool volume and volume/liquidity ratio from discovery aggregates."
    else:
        reasons["volume_confirmation"] = "Missing 24h volume or positive liquidity."

    if liquidity is not None and ratio is not None:
        filters["liquidity_depth"] = (
            "PASS" if liquidity >= 50_000 and 1 <= ratio <= 10 else "FAIL"
        )
        reasons["liquidity_depth"] = "Pool liquidity and turnover ratio only; executable depth is not yet measured."
    else:
        reasons["liquidity_depth"] = "Liquidity depth cannot be established from missing pool aggregates."

    if age is not None:
        filters["trend"] = "PASS" if age >= 24 else "FAIL"
        reasons["trend"] = "Pool age is known; this is not a price-trend confirmation."
    else:
        reasons["trend"] = "Pool creation timestamp is missing or invalid."

    if change is not None:
        filters["momentum"] = "FAIL" if change <= -20 or change >= 50 else "UNKNOWN"
        reasons["momentum"] = "Only 1h price change is available; multi-timeframe momentum is not established."
    else:
        reasons["momentum"] = "1h price change is unavailable."

    if buys is not None and sells is not None:
        if buys + sells >= 200 and buys + sells > 0:
            filters["market_integrity"] = "UNKNOWN"
            reasons["market_integrity"] = "Transaction count exists, but wash-trading and independent activity checks are absent."
        else:
            filters["market_integrity"] = "FAIL"
            reasons["market_integrity"] = "Observed 24h transaction count is below the 200-transaction floor."
    else:
        reasons["market_integrity"] = "24h buy/sell transaction counts are incomplete."

    if liquidity is not None and liquidity < 50_000:
        filters["liquidity_depth"] = "FAIL"

    reasons.setdefault("buy_sell_pressure", "Notional buy-volume share is unavailable; transaction counts are not a substitute.")
    reasons.setdefault("relative_strength", "No verified benchmark-relative price series is available.")
    reasons.setdefault("capital_flow", "No independently attributed net capital-flow evidence is available.")
    reasons.setdefault("whale_activity", "No attributable on-chain SWAP_BUY/SWAP_SELL history is connected.")
    reasons.setdefault("contract_security", "No verified contract bytecode, ownership, exploit, or honeypot assessment is connected.")
    reasons.setdefault("holder_distribution", "No independent holder balances or beneficial-owner clustering data is connected.")
    reasons.setdefault("market_regime_validation", "No benchmark/regime validation is attached to this DEX candidate.")

    gates = {
        "security": "UNKNOWN",
        "liquidity": (
            "PASS" if liquidity is not None and liquidity >= 50_000 and ratio is not None and 1 <= ratio <= 10
            else "FAIL" if liquidity is not None and liquidity < 50_000
            else "UNKNOWN"
        ),
        "data_quality": "UNKNOWN",
        "exit": "UNKNOWN",
    }

    # The only current discovery provider is GeckoTerminal. Multiple endpoint
    # names from one provider are not independent cross-source validation.
    sources = ["GECKOTERMINAL"]
    metrics = {
        "price_usd": _number(candidate.get("price_usd")),
        "liquidity_usd": liquidity,
        "volume_24h_usd": volume,
        "volume_to_liquidity_24h": ratio,
        "holder_count": None,
        "unique_traders_24h": None,
        "buy_volume_share_pct": None,
        "transactions_24h": (buys + sells) if buys is not None and sells is not None else None,
        "pool_age_hours": age,
        "top1_independent_holder_pct": None,
        "top10_independent_holders_pct": None,
        "related_wallet_cluster_pct": None,
        "estimated_slippage_bps": None,
    }
    whale = {
        "status": "UNKNOWN",
        "coverage_status": "UNKNOWN",
        "independent_buy_wallets": 0,
        "events": [],
        "reason": "No indexed, attributable token-lifetime swap history is connected.",
    }
    return {
        "schema_version": "gem-hunter-v1",
        "decision": "NO_TRADE",
        "market_type": "SPOT",
        "filters": filters,
        "filter_reasons": reasons,
        "critical_gates": gates,
        "opportunity_score": None,
        "risk_score": None,
        "data_as_of_utc": now.isoformat().replace("+00:00", "Z"),
        "market_data_sources": sources,
        "market_metrics": metrics,
        "whale_evidence": whale,
        "evaluation_status": "INCOMPLETE",
        "evaluation_reason": "Discovery currently supplies one DEX aggregate source only; independent market, security, holder, whale-swap and exit evidence are not connected.",
    }


def evaluate_discovery_candidates(candidates: Any, *, now: datetime | None = None) -> list[dict]:
    """Return copied candidates with explicit incomplete evaluation records."""
    if not isinstance(candidates, list):
        return []
    evaluated = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        row["qualification"] = evaluate_discovery_candidate(row, now=now)
        row["candidate_status"] = "UNSCREENED"
        row["trade_authorization"] = "NO_TRADE"
        evaluated.append(row)
    return evaluated
