"""Evidence-bounded first-stage evaluation for Gem Hunter discovery records.

This module evaluates only facts actually present in discovery payloads. It does
not infer holder distribution, contract safety, notional buy pressure, whale
swaps, or executable slippage from aggregate DEX pool statistics.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .token_risk import evaluate_token_security


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

def _validated_technical_evidence(candidate: dict, evidence: Any,
                                  now: datetime) -> dict[str, Any]:
    """Accept only fresh, complete OHLCV evidence for this exact token and pool."""
    if not isinstance(evidence, dict):
        return {}
    if evidence.get("schema_version") != "gem-hunter-technical-v1":
        return {}
    if evidence.get("status") != "COMPLETE" or evidence.get("coverage_status") != "COMPLETE":
        return {}
    chain = str(candidate.get("chain") or "").lower()
    if str(evidence.get("chain") or "").lower() != chain:
        return {}
    for key in ("token_address", "pool_address"):
        left, right = candidate.get(key), evidence.get(key)
        if not isinstance(left, str) or not isinstance(right, str):
            return {}
        if (left != right) if chain == "solana" else (left.lower() != right.lower()):
            return {}
    sources = set(evidence.get("sources") or [])
    if not {"GECKOTERMINAL", "BINANCE_SPOT"}.issubset(sources):
        return {}
    raw_timestamp = evidence.get("checked_at_utc")
    if not isinstance(raw_timestamp, str):
        return {}
    try:
        checked = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
    except ValueError:
        return {}
    if checked.tzinfo is None:
        return {}
    age = (now - checked.astimezone(timezone.utc)).total_seconds()
    if age < -30 or age > 300:
        return {}
    filters = evidence.get("filters")
    if not isinstance(filters, dict):
        return {}
    allowed = {"PASS", "FAIL", "UNKNOWN"}
    if any(filters.get(key) not in allowed for key in (
        "trend", "momentum", "relative_strength", "market_regime_validation"
    )):
        return {}
    return evidence


def evaluate_discovery_candidate(candidate: dict, *, now: datetime | None = None) -> dict:
    """Attach a conservative, explicit qualification record; never authorizes BUY."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)

    reconciliation = candidate.get("market_reconciliation")
    reconciliation = reconciliation if isinstance(reconciliation, dict) else {}
    reconciliation_status = reconciliation.get("status")
    # Keep the source-reported price visible separately; it is not verified evidence.
    reported_price = _number(candidate.get("price_usd"))
    reported_liquidity = _number(candidate.get("liquidity_usd"))
    reported_volume = _number(candidate.get("volume_24h_usd"))
    # Values from the reconciliation record count as verified only when its
    # exact-pool, two-provider status is PASS. Failed/partial observations stay
    # visible as reported values but never enter verified metrics or BUY inputs.
    verified_price = (
        _number(reconciliation.get("verified_price_usd"))
        if reconciliation_status == "PASS" else None
    )
    verified_liquidity = (
        _number(reconciliation.get("verified_liquidity_usd"))
        if reconciliation_status == "PASS" else None
    )
    verified_volume = (
        _number(reconciliation.get("verified_volume_24h_usd"))
        if reconciliation_status == "PASS" else None
    )
    liquidity = verified_liquidity if verified_liquidity is not None else reported_liquidity
    volume = verified_volume if verified_volume is not None else reported_volume
    ratio = (volume / liquidity if volume is not None and liquidity is not None and liquidity > 0
             else _number(candidate.get("volume_to_liquidity_24h")))
    age = _number(candidate.get("pool_age_hours"))
    buys = _number(candidate.get("buys_24h"))
    sells = _number(candidate.get("sells_24h"))
    change = _number(candidate.get("price_change_1h_pct"))
    technical_evidence = _validated_technical_evidence(
        candidate, candidate.get("technical_evidence"), now,
    )
    technical_filters = technical_evidence.get("filters", {})
    security_evidence = candidate.get("security_evidence")
    security_evidence = security_evidence if isinstance(security_evidence, dict) else {}
    token_risk_evidence = candidate.get("token_risk_evidence")
    if not isinstance(token_risk_evidence, dict):
        token_risk_evidence = {}
        # Preserve an explicit source-backed disqualifier, but do not translate
        # a limited provider PASS into full token-risk PASS.
        if (
            security_evidence.get("status") == "FAIL"
            and security_evidence.get("provider")
            and security_evidence.get("checked_at_utc")
            and (
                security_evidence.get("flags") or security_evidence.get("reason_codes")
                or security_evidence.get("reasons") or security_evidence.get("reason")
            )
        ):
            token_risk_evidence["contract_authority"] = {
                "status": "FAIL",
                "source": security_evidence.get("provider"),
                "checked_at": security_evidence.get("checked_at_utc"),
                "coverage_pct": None,
                "value": {
                    "flags": security_evidence.get("flags"),
                    "reason_codes": security_evidence.get("reason_codes"),
                    "reasons": security_evidence.get("reasons") or security_evidence.get("reason") or security_evidence.get("reason_fa"),
                    "mint_authority_active": security_evidence.get("mint_authority_active"),
                    "freeze_authority_active": security_evidence.get("freeze_authority_active"),
                },
                "reason": "A limited provider check identified an explicit security disqualifier.",
            }
    token_risk_assessment = evaluate_token_security(token_risk_evidence, now=now)
    security_status = token_risk_assessment["security_gate_status"]

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

    # Technical filters are upgraded only from fresh, complete, exact-pool
    # multi-timeframe OHLCV plus BTC/ETH benchmark evidence.
    if technical_evidence:
        for filter_id in ("trend", "momentum", "relative_strength", "market_regime_validation"):
            filters[filter_id] = technical_filters[filter_id]
        reasons["trend"] = (
            "روند با EMA20/EMA50 روی کندل‌های بسته‌شدهٔ ۴ساعته و روزانه تأیید شد."
            if filters["trend"] == "PASS" else
            "روند چندبازه‌ای نزولی است."
            if filters["trend"] == "FAIL" else
            "جهت روند در بازه‌های ۴ساعته و روزانه هم‌جهت نیست."
        )
        reasons["momentum"] = (
            "شتاب با بازده و RSI14 در بازه‌های ۱۵دقیقه و ۴ساعت تأیید شد."
            if filters["momentum"] == "PASS" else
            "شتاب در هر دو بازهٔ کوتاه‌مدت و میان‌مدت منفی است."
            if filters["momentum"] == "FAIL" else
            "شرایط شتاب در تمام بازه‌های لازم هم‌جهت نیست."
        )
        reasons["relative_strength"] = (
            "بازده ۷ و ۳۰روزهٔ توکن از BTC و ETH در هر دو پنجره بیشتر است."
            if filters["relative_strength"] == "PASS" else
            "بازده ۷ و ۳۰روزهٔ توکن از BTC و ETH در هر دو پنجره کمتر است."
            if filters["relative_strength"] == "FAIL" else
            "قدرت نسبی در بازه‌های ۷ و ۳۰روزه با هر دو benchmark هم‌جهت نیست."
        )
        reasons["market_regime_validation"] = (
            "رژیم روزانهٔ BTC و ETH هر دو صعودی است."
            if filters["market_regime_validation"] == "PASS" else
            "رژیم روزانهٔ BTC و ETH هر دو نزولی است."
            if filters["market_regime_validation"] == "FAIL" else
            "رژیم BTC و ETH برای تأیید یک جهت مشترک هم‌سو نیست."
        )
    else:
        reasons["trend"] = "Pool age یا تغییر یک‌ساعته روند محسوب نمی‌شود؛ تاریخچهٔ چندبازه‌ای تازه و کامل متصل نیست."
        reasons["momentum"] = "شتاب فقط با OHLCV بسته‌شدهٔ ۱۵دقیقه‌ای و ۴ساعته و RSI14 ارزیابی می‌شود؛ شواهد کامل در دسترس نیست."

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
                reconciliation_status != "PASS"
                and reported_liquidity is not None and reported_liquidity < 50_000
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

    # Only independently verified exact-pool sources are listed here; discovery
    # feed provenance remains on the candidate itself.
    sources = sorted({
        str(row.get("provider")) for row in reconciliation.get("sources", [])
        if isinstance(row, dict) and row.get("provider")
    }) if reconciliation_status == "PASS" else []
    metrics = {
        # These canonical fields are verified-only. Raw discovery observations
        # are exposed separately so the UI cannot label them as cross-checked.
        "price_usd": verified_price,
        "verified_price_usd": verified_price,
        "observed_price_usd": reported_price,
        "price_verification_status": "PASS" if verified_price is not None and verified_price > 0 else "UNKNOWN",
        "liquidity_usd": verified_liquidity,
        "verified_liquidity_usd": verified_liquidity,
        "observed_liquidity_usd": reported_liquidity,
        "reported_liquidity_usd": reported_liquidity,
        "volume_24h_usd": verified_volume,
        "verified_volume_24h_usd": verified_volume,
        "observed_volume_24h_usd": reported_volume,
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
        "technical_evidence_status": technical_evidence.get("status", "UNKNOWN") if technical_evidence else "UNKNOWN",
        "technical_metrics": technical_evidence.get("metrics", {}) if technical_evidence else {},
        "technical_timeframes": technical_evidence.get("timeframes", {}) if technical_evidence else {},
    }
    whale = {
        "status": "UNKNOWN",
        "coverage_status": "UNKNOWN",
        "independent_buy_wallets": 0,
        "events": [],
        "reason": "No indexed, attributable token-lifetime swap history is connected.",
    }
    return {
        "schema_version": "gem-hunter-v2",
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
        "token_risk_assessment": token_risk_assessment,
        "evaluation_reason": "Exact-pool market data is independently reconciled where available; complete source-backed contract, holder, deployer, launch-bundle and executable-exit evidence is required for the security sub-gate.",
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
