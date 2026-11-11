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

    reconciliation = candidate.get("market_reconciliation")
    reconciliation = reconciliation if isinstance(reconciliation, dict) else {}
    reconciliation_status = reconciliation.get("status")
    reported_liquidity = _number(candidate.get("liquidity_usd"))
    reported_volume = _number(candidate.get("volume_24h_usd"))
    verified_liquidity = _number(reconciliation.get("verified_liquidity_usd"))
    verified_volume = _number(reconciliation.get("verified_volume_24h_usd"))
    liquidity = verified_liquidity if verified_liquidity is not None else reported_liquidity
    volume = verified_volume if verified_volume is not None else reported_volume
    ratio = (volume / liquidity if volume is not None and liquidity is not None and liquidity > 0
             else _number(candidate.get("volume_to_liquidity_24h")))
    age = _number(candidate.get("pool_age_hours"))
    buys = _number(candidate.get("buys_24h"))
    sells = _number(candidate.get("sells_24h"))
    change = _number(candidate.get("price_change_1h_pct"))
    security_evidence = candidate.get("security_evidence")
    security_evidence = security_evidence if isinstance(security_evidence, dict) else {}
    security_status = security_evidence.get("status")
    if security_status not in {"PASS", "FAIL"}:
        security_status = "UNKNOWN"

    filters = {name: "UNKNOWN" for name in FILTER_IDS}
    filters["contract_security"] = security_status
    reasons = {
        "contract_security": (
            "بررسی حداقلی قرارداد از GoPlus و Sourcify عبور کرده است؛ این ممیزی کامل نیست."
            if security_status == "PASS" else
            "بررسی قرارداد یک ریسک یا اختیار حساس را شناسایی کرده است."
            if security_status == "FAIL" else
            "شواهد امنیت قرارداد ناقص است یا پوشش منبع برای صدور حکم کافی نیست."
        )
    }
    reasons = {}

    # GeckoTerminal exposes transaction counts, not buy/sell notional volume.
    # Keep that distinction explicit: transaction share is not buy-volume share.
    if reconciliation_status == "FAIL":
        filters["volume_confirmation"] = "FAIL"
        reasons["volume_confirmation"] = "Independent exact-pool market reconciliation failed."
    elif reconciliation_status == "PASS" and volume is not None and liquidity is not None and liquidity > 0:
        filters["volume_confirmation"] = (
            "PASS" if volume >= 100_000 and ratio is not None and 1 <= ratio <= 10
            else "FAIL"
        )
        reasons["volume_confirmation"] = "24h volume and turnover ratio were reconciled for the exact pool across independent sources."
    else:
        reasons["volume_confirmation"] = "Volume and liquidity have not been independently reconciled for the exact pool."

    if reconciliation_status == "FAIL" or (
        liquidity is not None and liquidity < 50_000
        and (reconciliation_status == "PASS" or reported_liquidity is not None)
    ):
        filters["liquidity_depth"] = "FAIL"
        reasons["liquidity_depth"] = "Exact-pool reconciliation failed or verified pool liquidity is below the minimum floor."
    else:
        reasons["liquidity_depth"] = "Pool liquidity aggregate is not executable order-book/pool depth; depth remains UNKNOWN."

    # Pool age is not a trend signal. A price series with multiple timeframes
    # is required before this filter can pass or fail.
    reasons["trend"] = "Pool age is not a trend signal; verified multi-timeframe price history is not connected."

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

    reasons.setdefault("buy_sell_pressure", "Notional buy-volume share is unavailable; transaction counts are not a substitute.")
    reasons.setdefault("relative_strength", "No verified benchmark-relative price series is available.")
    reasons.setdefault("capital_flow", "No independently attributed net capital-flow evidence is available.")
    reasons.setdefault("whale_activity", "No attributable on-chain SWAP_BUY/SWAP_SELL history is connected.")
    reasons.setdefault("contract_security", "No verified contract bytecode, ownership, exploit, or honeypot assessment is connected.")
    reasons.setdefault("holder_distribution", "No independent holder balances or beneficial-owner clustering data is connected.")
    reasons.setdefault("market_regime_validation", "No benchmark/regime validation is attached to this DEX candidate.")

    gates = {
        "security": security_status,
        # Aggregate liquidity is not trusted until the exact token/pool pair
        # matches across DexScreener and GeckoTerminal.
        "liquidity": (
            "FAIL" if reconciliation_status == "FAIL" or (
                reported_liquidity is not None and reported_liquidity < 50_000
            )
            else "PASS" if reconciliation_status == "PASS"
            and liquidity is not None and liquidity >= 50_000
            and ratio is not None and 1 <= ratio <= 10
            else "UNKNOWN"
        ),
        "data_quality": (
            "PASS" if reconciliation_status == "PASS"
            else "FAIL" if reconciliation_status == "FAIL"
            else "UNKNOWN"
        ),
        "exit": "UNKNOWN",
    }

    gate_reasons = {
        "security": (
            "بررسی حداقلی GoPlus و Sourcify عبور کرده است؛ این نتیجه ممیزی کامل یا تضمین امنیت نیست."
            if security_status == "PASS" else
            "منبع بررسی قرارداد ریسک امنیتی یا اختیار مدیریتی حساس گزارش کرده است."
            if security_status == "FAIL" else
            "امنیت قرارداد با دادهٔ فعلی قابل تأیید نیست؛ UNKNOWN."
        ),
        "liquidity": (
            "نقدینگی و حجم Pool دقیق در دو منبع مستقل تطبیق داده شده‌اند؛ این وضعیت عمق اجرایی یا امکان خروج را اثبات نمی‌کند."
            if gates["liquidity"] == "PASS" else
            "تطبیق مستقل Pool شکست خورده یا نقدینگی تأییدشده زیر حد اولیه است."
            if gates["liquidity"] == "FAIL" else
            "تطبیق دو منبع مستقل برای همین Pool تکمیل نشده؛ نقدینگی تأییدنشده است."
        ),
        "data_quality": (
            "هویت توکن و Pool و نقدینگی/حجم در دو منبع مستقل تطبیق داده شده است؛ سایر شواهد همچنان باید تازه‌سازی شوند."
            if gates["data_quality"] == "PASS" else
            "اختلاف داده یا هویت Pool بین منابع مستقل شناسایی شد."
            if gates["data_quality"] == "FAIL" else
            "تطبیق دو منبع مستقل برای همین Pool هنوز کامل نیست."
        ),
        "exit": "عمق اجرایی و لغزش برای اندازهٔ سفارش مشخص نیست؛ امکان خروج قابل تأیید نیست.",
    }

    # The only current discovery provider is GeckoTerminal. Multiple endpoint
    # names from one provider are not independent cross-source validation.
    sources = sorted({
        str(row.get("provider")) for row in reconciliation.get("sources", [])
        if isinstance(row, dict) and row.get("provider")
    }) or ["GECKOTERMINAL"]
    metrics = {
        "price_usd": _number(reconciliation.get("verified_price_usd")) or _number(candidate.get("price_usd")),
        "liquidity_usd": liquidity,
        "reported_liquidity_usd": reported_liquidity,
        "volume_24h_usd": volume,
        "reported_volume_24h_usd": reported_volume,
        "market_reconciliation_status": reconciliation_status,
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
        "critical_gate_reasons": gate_reasons,
        "opportunity_score": None,
        "risk_score": None,
        "data_as_of_utc": now.isoformat().replace("+00:00", "Z"),
        "market_data_sources": sources,
        "market_metrics": metrics,
        "whale_evidence": whale,
        "evaluation_status": "INCOMPLETE",
        "market_reconciliation": reconciliation,
        "security_evidence": security_evidence,
        "evaluation_reason": "Exact-pool market data is independently reconciled where available; contract security, holder ownership, notional flow, attributable whale swaps and executable exit depth still require separate evidence.",
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
        # Existing evaluator output is authoritative input to the strict public
        # qualification gate; never overwrite or upgrade it here.
        if not isinstance(row.get("qualification"), dict):
            row["qualification"] = evaluate_discovery_candidate(row, now=now)
            row["candidate_status"] = "UNSCREENED"
            row["trade_authorization"] = "NO_TRADE"
        evaluated.append(row)
    return evaluated
