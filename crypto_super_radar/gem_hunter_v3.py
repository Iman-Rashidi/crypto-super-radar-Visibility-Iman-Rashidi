"""Gem Hunter V3 research-only lifecycle, exit-liquidity and alert primitives.

No function in this module submits trades or changes Futures automation.
"""
from __future__ import annotations

import hashlib
import json
import os
from html import escape as html_escape
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

import requests

from .database import connect_database, storage_backend


SCHEMA_VERSION = "gem-hunter-v3"
DEFAULT_EXIT_ORDER_USD = 500.0
MAX_EXIT_SLIPPAGE_BPS = 100.0


def _finite(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def estimate_exit_slippage(
    *, liquidity_usd: Any, order_usd: Any = DEFAULT_EXIT_ORDER_USD,
    fee_bps: float = 30.0, max_slippage_bps: float = MAX_EXIT_SLIPPAGE_BPS,
) -> dict[str, Any]:
    """Conservative constant-product estimate; liquidity_usd is assumed to be pool TVL.

    The pool is assumed balanced, so quote-side reserve is TVL/2. This is an
    estimate, not a routed quote. Missing/invalid reserves remain UNKNOWN.
    """
    liquidity = _finite(liquidity_usd)
    order = _finite(order_usd)
    fee = _finite(fee_bps)
    if liquidity is None or order is None or fee is None or liquidity <= 0 or order <= 0 or fee < 0:
        return {"status": "UNKNOWN", "estimated_slippage_bps": None,
                "reason": "نقدینگی، اندازه سفارش یا کارمزد معتبر نیست."}
    reserve = liquidity / 2.0
    if order >= reserve:
        return {"status": "FAIL", "estimated_slippage_bps": None,
                "reason": "اندازه سفارش به ذخیره فرضی سمت خروج می‌رسد یا از آن بیشتر است."}
    price_impact = order / (reserve + order)
    total_slippage = 1.0 - ((1.0 - price_impact) * (1.0 - fee / 10000.0))
    bps = round(total_slippage * 10000.0, 2)
    status = "PASS" if bps <= max_slippage_bps else "FAIL"
    return {
        "status": status,
        "estimated_slippage_bps": bps,
        "order_usd": order,
        "assumed_quote_reserve_usd": round(reserve, 2),
        "fee_bps": fee,
        "model": "CONSTANT_PRODUCT_BALANCED_POOL_ESTIMATE",
        "reason": ("لغزش تخمینی در محدوده مجاز است." if status == "PASS"
                   else "لغزش تخمینی از حد مجاز بیشتر است."),
        "trade_authorization": "NO_TRADE",
    }


def score_candidate_v3(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Score observable momentum; never mislabel bounded wallet data as smart money."""
    trades = candidate.get("pool_trade_evidence") or {}
    technical = candidate.get("technical_evidence") or {}
    security = candidate.get("security_evidence") or {}
    exit_gate = candidate.get("exit_slippage_evidence") or {}
    reconciliation = candidate.get("market_reconciliation") or {}
    qualification = candidate.get("qualification") or {}
    qualification_metrics = qualification.get("market_metrics") or {}

    components: dict[str, float] = {}
    buy = _finite(trades.get("buy_volume_usd"))
    sell = _finite(trades.get("sell_volume_usd"))
    if (trades.get("coverage_status") in {"COMPLETE", "PARTIAL"}
            and buy is not None and sell is not None and buy >= 0 and sell >= 0 and buy + sell > 0):
        components["pool_buy_volume_share"] = round(100.0 * buy / (buy + sell), 2)

    unique_buyers = _finite(trades.get("unique_buyers_count"))
    unique_sellers = _finite(trades.get("unique_sellers_count"))
    wallet_sample_verified = (
        trades.get("wallet_attribution_status") == "COMPLETE_FOR_RETURNED_SAMPLE"
        and unique_buyers is not None and unique_sellers is not None
        and unique_buyers >= 0 and unique_sellers >= 0
        and unique_buyers + unique_sellers > 0
    )
    if wallet_sample_verified:
        components["observed_buyer_wallet_share"] = round(
            100.0 * unique_buyers / (unique_buyers + unique_sellers), 2
        )

    filters = technical.get("filters") if isinstance(technical, Mapping) else {}
    technical_parts = [
        100.0 if filters.get(key) == "PASS" else 0.0
        for key in ("trend", "momentum") if filters.get(key) in {"PASS", "FAIL"}
    ] if isinstance(filters, Mapping) else []
    if technical_parts and technical.get("coverage_status") in {"COMPLETE", "PARTIAL"}:
        components["technical_trend_momentum"] = round(sum(technical_parts) / len(technical_parts), 2)

    # Explicit weights; missing evidence is excluded from the denominator and
    # separately exposed as coverage. Unique wallet counts are sample breadth,
    # not new-buyer cohorts, beneficial-owner identities, or smart-money proof.
    weights = {
        "pool_buy_volume_share": 0.50,
        "observed_buyer_wallet_share": 0.25,
        "technical_trend_momentum": 0.25,
    }
    available_weight = sum(weights[key] for key in components)
    momentum = (
        round(sum(components[key] * weights[key] for key in components) / available_weight, 2)
        if available_weight > 0 else None
    )
    momentum_coverage_pct = round(available_weight * 100.0, 2)

    # DEX transaction counts are an explicitly labelled proxy, not unique wallets.
    buys_24h = _finite(candidate.get("buys_24h"))
    sells_24h = _finite(candidate.get("sells_24h"))
    dex_buy_share = None
    if buys_24h is not None and sells_24h is not None and buys_24h >= 0 and sells_24h >= 0 and buys_24h + sells_24h > 0:
        dex_buy_share = round(100.0 * buys_24h / (buys_24h + sells_24h), 2)
    if momentum is None and dex_buy_share is not None:
        momentum = dex_buy_share
        momentum_coverage_pct = 25.0
        components["dex_transaction_buy_share_estimate"] = dex_buy_share

    unique_trader_count = _finite(trades.get("unique_trader_count"))
    unique_trader_basis = "POOL_PROVIDER_UNIQUE_TRADERS"
    if unique_trader_count is None:
        unique_trader_count = _finite(qualification_metrics.get("unique_traders_24h"))
        if unique_trader_count is not None:
            unique_trader_basis = "QUALIFICATION_MARKET_METRICS_REPORTED"
    if unique_trader_count is None:
        unique_buyers = _finite(trades.get("unique_buyers_count"))
        unique_sellers = _finite(trades.get("unique_sellers_count"))
        if trades.get("wallet_attribution_status") == "COMPLETE_FOR_RETURNED_SAMPLE" and unique_buyers is not None and unique_sellers is not None:
            unique_trader_count = unique_buyers + unique_sellers
            unique_trader_basis = "UNIQUE_WALLETS_IN_RETURNED_SAMPLE"
        elif buys_24h is not None and sells_24h is not None:
            unique_trader_count = buys_24h + sells_24h
            unique_trader_basis = "TRANSACTION_COUNT_PROXY_NOT_UNIQUE_WALLETS"

    buy_volume_share_pct = _finite(trades.get("buy_volume_share_pct"))
    if buy_volume_share_pct is None:
        buy_volume_share_pct = _finite(qualification_metrics.get("buy_volume_share_pct"))
    if buy_volume_share_pct is None:
        buy_volume_share_pct = _finite(components.get("pool_buy_volume_share"))
    if buy_volume_share_pct is None:
        buy_volume_share_pct = dex_buy_share

    holder_evidence = candidate.get("holder_evidence") or candidate.get("holders") or {}
    holder_count = _finite(candidate.get("holder_count", security.get("holder_count", holder_evidence.get("holder_count", security.get("largest_accounts_count", qualification_metrics.get("holder_count"))))))
    if holder_count is None:
        holder_count = _finite(qualification_metrics.get("holder_count"))
    if holder_count is not None:
        holder_count = int(holder_count)


    security_status = security.get("status")
    exit_status = exit_gate.get("status")
    safety = None
    if security_status == "FAIL" or exit_status == "FAIL":
        safety = 0.0
    elif security_status == "PASS" and exit_status == "PASS":
        slip = _finite(exit_gate.get("estimated_slippage_bps"))
        safety = round(max(0.0, 100.0 - (slip or 0.0) / MAX_EXIT_SLIPPAGE_BPS * 30.0), 2)

    # Independent risk score: missing evidence raises risk, never implies PASS.
    risk_score = 55.0 if safety is None else round(100.0 - safety, 2)
    if security_status != "PASS":
        risk_score += 20.0
    if exit_status == "UNKNOWN":
        risk_score += 10.0
    elif exit_status == "FAIL":
        risk_score += 20.0
    volume_liquidity = _finite(candidate.get("volume_to_liquidity_24h"))
    if volume_liquidity is None:
        liq = _finite(reconciliation.get("verified_liquidity_usd")) or _finite(candidate.get("liquidity_usd"))
        vol = _finite(reconciliation.get("verified_volume_24h_usd")) or _finite(candidate.get("volume_24h_usd"))
        if liq is not None and liq > 0 and vol is not None:
            volume_liquidity = vol / liq
    if volume_liquidity is not None and volume_liquidity > 5:
        risk_score += min(15.0, (volume_liquidity - 5.0) * 2.0)
    if holder_count is None:
        risk_score += 5.0
    risk_score = round(min(100.0, max(0.0, risk_score)), 2)

    confidence_parts = []
    if reconciliation.get("status") in {"PASS", "FAIL"}:
        if reconciliation.get("status") == "FAIL":
            confidence_parts.append(0.0)
        elif reconciliation.get("verification_level") == "DEXSCREENER_PRIMARY_SINGLE_SOURCE":
            confidence_parts.append(50.0)
        else:
            confidence_parts.append(100.0)
    if technical.get("coverage_status") in {"COMPLETE", "PARTIAL"}:
        confidence_parts.append(100.0 if technical.get("coverage_status") == "COMPLETE" else 50.0)
    if trades.get("coverage_status") in {"COMPLETE", "PARTIAL"}:
        confidence_parts.append(100.0 if trades.get("coverage_status") == "COMPLETE" else 50.0)
    if wallet_sample_verified:
        confidence_parts.append(100.0)
    if security_status in {"PASS", "FAIL"}:
        confidence_parts.append(100.0)
    confidence = round(sum(confidence_parts) / len(confidence_parts), 2) if confidence_parts else None

    verified_liquidity = _finite(reconciliation.get("verified_liquidity_usd"))
    verified_volume = _finite(reconciliation.get("verified_volume_24h_usd"))
    observed_pool_buy_share = _finite(components.get("pool_buy_volume_share"))
    qualified = (
        momentum is not None and momentum >= 70.0 and momentum_coverage_pct >= 75.0
        and observed_pool_buy_share is not None and observed_pool_buy_share >= 55.0
        and verified_liquidity is not None and verified_liquidity >= 50_000
        and verified_volume is not None and verified_volume >= 100_000
        and safety is not None and safety >= 70.0
        and confidence is not None and confidence >= 75.0
        and security_status == "PASS" and exit_status == "PASS"
        and reconciliation.get("status") == "PASS"
        and reconciliation.get("verification_level") == "INDEPENDENT_CROSS_SOURCE"
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "short_term_momentum_score": momentum,
        "opportunity_score": momentum,
        "risk_score": risk_score,
        "buy_volume_share_pct": buy_volume_share_pct,
        "unique_traders_24h": int(unique_trader_count) if unique_trader_count is not None else None,
        "unique_traders_24h_basis": unique_trader_basis if unique_trader_count is not None else "NOT_AVAILABLE",
        "holder_count": holder_count,
        "holder_count_basis": ("SOLANA_LARGEST_TOKEN_ACCOUNTS_SAMPLE" if str(candidate.get("chain") or "").lower() == "solana" and holder_count is not None else "PROVIDER_HOLDER_COUNT" if holder_count is not None else "NOT_AVAILABLE"),
        "estimated_slippage_bps": _finite(exit_gate.get("estimated_slippage_bps")) if _finite(exit_gate.get("estimated_slippage_bps")) is not None else _finite(qualification_metrics.get("estimated_slippage_bps")),
        # Compatibility alias for the existing dashboard. This is NOT a
        # smart-money score; see the explicit evidence status below.
        "short_term_momentum_smart_money_score": momentum,
        "momentum_score_components": components,
        "momentum_evidence_coverage_pct": momentum_coverage_pct,
        "wallet_breadth_evidence_status": "VERIFIED_FOR_RETURNED_SAMPLE" if wallet_sample_verified else "UNKNOWN",
        "smart_money_evidence_status": "NOT_AVAILABLE",
        "new_buyer_cohort_status": "NOT_AVAILABLE",
        "safety_exit_liquidity_score": safety,
        "data_confidence_score": confidence,
        "candidate_status": "QUALIFIED_GEM" if qualified else "RESEARCH_WATCH",
        "trade_authorization": "NO_TRADE",
        "score_semantics": (
            "Observed Pool buy-volume share, returned-sample wallet breadth and technical filters; "
            "not new-buyer cohort analysis, beneficial-owner attribution, smart-money tracking, "
            "a probability of profit or trade instruction."
        ),
    }



def _base_security_status(chain: str, security: Mapping[str, Any]) -> str:
    """Assess only explicit anti-honeypot/sellability basics; do not require secondary analytics."""
    if security.get("status") == "PASS":
        return "PASS"
    flags = security.get("flags") or {}
    sell_tax = _finite(security.get("sell_tax_pct"))
    if chain == "solana":
        checks = (
            security.get("mint_authority_active") is False,
            security.get("freeze_authority_active") is False,
            security.get("honeypot_detected") is False,
            sell_tax is not None and 0 <= sell_tax <= 5.0,
        )
        return "PASS" if all(checks) else "UNKNOWN"
    if chain in {"base", "ethereum"}:
        required_false = (
            "is_honeypot", "cannot_sell_all", "is_blacklisted", "is_mintable",
            "transfer_pausable", "hidden_owner", "owner_change_balance",
            "can_take_back_ownership", "selfdestruct",
        )
        checks = [flags.get(key) is False for key in required_false]
        checks.extend([
            security.get("source_verified") is True,
            sell_tax is not None and 0 <= sell_tax <= 5.0,
        ])
        return "PASS" if all(checks) else "UNKNOWN"
    return "UNKNOWN"


def classify_gem_tier(candidate: Mapping[str, Any], scores: Mapping[str, Any]) -> dict[str, Any]:
    """Keep promising but incomplete discoveries visible without weakening hard safety gates."""
    security = candidate.get("security_evidence") or {}
    exit_gate = candidate.get("exit_slippage_evidence") or {}
    market = candidate.get("market_reconciliation") or {}
    flags = security.get("flags") or {}
    chain = str(candidate.get("chain") or "").lower()
    base_security_status = _base_security_status(chain, security)
    liquidity = _finite(
        market.get("verified_liquidity_usd") if market.get("status") == "PASS"
        else candidate.get("liquidity_usd")
    )
    volume = _finite(
        market.get("verified_volume_24h_usd") if market.get("status") == "PASS"
        else candidate.get("volume_24h_usd")
    )
    ratio = _finite(candidate.get("volume_to_liquidity_24h"))
    if ratio is None and liquidity and volume is not None:
        ratio = volume / liquidity if liquidity > 0 else None

    trades = candidate.get("pool_trade_evidence") or {}
    components = scores.get("momentum_score_components") or {}
    buy_share = _finite(components.get("pool_buy_volume_share"))
    if buy_share is None:
        buy_share = _finite(candidate.get("buy_volume_share_pct"))
    buys = _finite(candidate.get("buys_24h"))
    sells = _finite(candidate.get("sells_24h"))
    tx_buy_share = (
        100.0 * buys / (buys + sells)
        if buys is not None and sells is not None and buys >= 0 and sells >= 0 and buys + sells > 0
        else None
    )
    if buy_share is None:
        buy_share = tx_buy_share
    momentum = _finite(scores.get("short_term_momentum_score"))
    if momentum is None and buy_share is not None:
        momentum = round(buy_share, 2)

    hard_sell_block = (
        flags.get("is_honeypot") is True
        or flags.get("cannot_sell_all") is True
        or flags.get("is_blacklisted") is True
        or security.get("honeypot_detected") is True
        or security.get("transfer_simulation_status") == "FAIL"
        or (_finite(security.get("sell_tax_pct")) is not None and _finite(security.get("sell_tax_pct")) > 5.0)
    )
    if hard_sell_block:
        return {
            "tier": "BLOCKED_SCAM", "tier_label_fa": "مسدود: خطر قطعی فروش/کلاهبرداری",
            "tier_strengths_fa": [], "tier_cautions_fa": ["منبع امنیتی نشانهٔ صریح Honeypot، محدودیت فروش یا مالیات فروش مخرب گزارش کرده است."],
            "tier_data_confidence": "SOURCE_CONFIRMED",
            "tier_base_security_status": "FAIL",
        }
    if liquidity is not None and liquidity < 5_000:
        return {
            "tier": "BLOCKED_LOW_LIQUIDITY", "tier_label_fa": "مسدود: نقدینگی بسیار کم",
            "tier_strengths_fa": [], "tier_cautions_fa": ["نقدینگی کمتر از کف ۵٬۰۰۰ دلار است؛ این برچسب به‌معنی اثبات کلاهبرداری نیست."],
            "tier_data_confidence": "OBSERVED",
            "tier_base_security_status": base_security_status,
        }

    if market.get("status") == "FAIL":
        return {
            "tier": "REVIEW_REQUIRED", "tier_label_fa": "در انتظار رفع اختلاف منابع",
            "tier_strengths_fa": [], "tier_cautions_fa": ["دادهٔ بازار هنگام اعتبارسنجی با Snapshot اولیه یا هویت Pool سازگار نیست؛ تا تطبیق مجدد به Tier 2 ارتقا نمی‌یابد."],
            "tier_data_confidence": "CONFLICTED",
        }
    if exit_gate.get("status") == "FAIL":
        return {
            "tier": "REVIEW_REQUIRED", "tier_label_fa": "در انتظار بررسی امکان خروج",
            "tier_strengths_fa": [], "tier_cautions_fa": ["تخمین لغزش خروج از حد مجاز بیشتر است؛ حتی با حجم بالا در واچ‌لیست داغ قرار نمی‌گیرد."],
            "tier_data_confidence": "RISK_FLAG",
        }

    security_status = security.get("status")
    price = _finite(candidate.get("price_usd"))
    cheap_meme_candidate = (
        price is not None and 0 < price < 0.05
        and liquidity is not None and liquidity >= 25_000
        and volume is not None and volume > 100_000
    )
    if cheap_meme_candidate and base_security_status != "PASS":
        return {
            "tier": "EARLY_ALPHA_REVIEW",
            "tier_label_fa": "بررسی فرصت زودهنگام: امنیت هنوز تأیید نشده",
            "tier_strengths_fa": ["قیمت زیر ۰٫۰۵ دلار است.", "نقدینگی حداقل ۲۵٬۰۰۰ دلار و حجم ۲۴ساعته بیش از ۱۰۰٬۰۰۰ دلار گزارش شده است."],
            "tier_cautions_fa": ["وضعیت Honeypot، امکان فروش یا مالیات فروش هنوز با شواهد کافی تأیید نشده؛ این توکن Tier 2 نیست و نباید سالم فرض شود."],
            "tier_data_confidence": "INCOMPLETE",
            "tier_base_security_status": base_security_status,
            "gem_hunter_v3_trade_authorization": "NO_TRADE",
        }
    if base_security_status != "PASS" or security_status == "FAIL":
        return {
            "tier": "REVIEW_REQUIRED", "tier_label_fa": "در انتظار بررسی امنیت",
            "tier_strengths_fa": (["شتاب حجم و نقدینگی قابل توجه است."] if volume and liquidity and liquidity > 0 and volume / liquidity >= 3 else []),
            "tier_cautions_fa": ["شواهد پایهٔ ضد Honeypot/فروش هنوز کامل و قابل تأیید نیست؛ UNKNOWN به معنی سالم بودن قرارداد نیست."],
            "tier_data_confidence": "INCOMPLETE",
            "tier_base_security_status": base_security_status,
        }

    if cheap_meme_candidate:
        estimated_tokens = int(100.0 / price)
        return {
            "tier": "TIER_2_EARLY_ALPHA",
            "tier_label_fa": "میم‌کوین ارزان: فرصت اولیه پژوهشی · Tier 2",
            "tier_strengths_fa": [
                "قیمت زیر ۰٫۰۵ دلار است.",
                "نقدینگی حداقل ۲۵٬۰۰۰ دلار و حجم ۲۴ساعته بیش از ۱۰۰٬۰۰۰ دلار است.",
                "کنترل‌های پایه ضد Honeypot و مالیات فروش با شواهد موجود عبور کرده‌اند.",
            ],
            "tier_cautions_fa": [
                "این مسیر برای کشف زودهنگام است؛ نبود کندل تاریخی یا منبع دوم به معنی تأیید روند یا عمق واقعی خروج نیست.",
                "تعداد توکن با ۱۰۰ دلار تخمینی است و کارمزد، لغزش، مالیات احتمالی و تغییر قیمت را لحاظ نمی‌کند.",
                "Tier 2 توصیه خرید نیست و مجوز معامله ندارد.",
            ],
            "tier_data_confidence": "LIMITED",
            "tier_base_security_status": base_security_status,
            "estimated_tokens_for_100_usd": estimated_tokens,
            "estimated_budget_usd": 100,
            "gem_hunter_v3_trade_authorization": "NO_TRADE",
        }

    if (
        market.get("status") == "PASS"
        and market.get("verification_level") == "INDEPENDENT_CROSS_SOURCE"
        and exit_gate.get("status") == "PASS"
        and liquidity is not None and liquidity >= 50_000
        and volume is not None and volume >= 100_000
        and momentum is not None and momentum >= 70
        and _finite(scores.get("momentum_evidence_coverage_pct")) is not None
        and _finite(scores.get("momentum_evidence_coverage_pct")) >= 75
        and buy_share is not None and buy_share >= 55
        and _finite(scores.get("safety_exit_liquidity_score")) is not None
        and _finite(scores.get("safety_exit_liquidity_score")) >= 70
    ):
        return {
            "tier": "TIER_1_VERIFIED_GEM", "tier_label_fa": "ستاره طلایی: شواهد قوی و تطبیق‌شده",
            "tier_strengths_fa": ["امنیت پایه تأیید شده است.", "نقدینگی و حجم از دو منبع مستقل تطبیق شده‌اند.", "فشار خرید و امکان خروج تخمینی از آستانه عبور کرده‌اند."],
            "tier_cautions_fa": ["امتیاز رشد تضمین سود نیست؛ لغزش خروج هنوز تخمینی است."],
            "tier_data_confidence": "HIGH",
            "tier_base_security_status": base_security_status,
        }

    if (
        liquidity is not None and liquidity >= 10_000
        and volume is not None and volume >= 100_000
        and ratio is not None and ratio >= 3.0
        and buy_share is not None and buy_share >= 50
        and (momentum is None or momentum >= 50)
    ):
        cautions = []
        if security_status != "PASS":
            cautions.append("کنترل‌های پایهٔ Honeypot و مالیات فروش عبور کرده‌اند، اما بررسی تکمیلی فروش/هولدرها هنوز کامل نیست.")
        if market.get("status") != "PASS":
            cautions.append("تطبیق مستقل دو منبع هنوز کامل نیست؛ قیمت/حجم ممکن است نیازمند تأیید باشد.")
        if market.get("verification_level") == "DEXSCREENER_PRIMARY_SINGLE_SOURCE":
            cautions.append("قیمت، نقدینگی و حجم فعلاً فقط از DEX Screener تأیید شده‌اند؛ منبع مستقل دوم هنوز بررسی نشده است.")
        if exit_gate.get("status") != "PASS":
            cautions.append("لغزش خروج هنوز تأیید نشده؛ این طبقه مجوز معامله نیست.")
        if momentum is None or _finite(scores.get("momentum_evidence_coverage_pct")) is None or _finite(scores.get("momentum_evidence_coverage_pct")) < 75:
            cautions.append("سابقهٔ کندلی یا نمونهٔ معاملات هنوز کوتاه است؛ امتیاز مومنتوم پوشش کامل ندارد.")
        if scores.get("smart_money_evidence_status") == "NOT_AVAILABLE":
            cautions.append("انتساب پول هوشمند و شناسایی خریداران جدید هنوز در دسترس نیست.")
        return {
            "tier": "TIER_2_EARLY_ALPHA", "tier_label_fa": "فرصت اولیه: واچ‌لیست داغ",
            "tier_strengths_fa": ["گیت امنیت پایه PASS شده است.", f"نسبت حجم به نقدینگی {ratio:.2f} برابر است.", "حجم و فشار خرید اولیه از آستانهٔ واچ‌لیست عبور کرده‌اند."],
            "tier_cautions_fa": cautions or ["فرصت اولیه است و هنوز تأیید کامل Tier 1 را ندارد."],
            "tier_data_confidence": "MEDIUM" if market.get("verification_level") == "INDEPENDENT_CROSS_SOURCE" and security_status == "PASS" else "LIMITED",
            "tier_base_security_status": base_security_status,
        }

    return {
        "tier": "RESEARCH_WATCH", "tier_label_fa": "پایش پژوهشی: شواهد هنوز کافی نیست",
        "tier_strengths_fa": (["امنیت پایه PASS شده است."] if security_status == "PASS" else []),
        "tier_cautions_fa": ["نامزد حذف نشده است؛ برای ارتقا به Tier 2 به نقدینگی، حجم و فشار خرید قابل‌اندازه‌گیری نیاز دارد."],
        "tier_data_confidence": "LIMITED",
        "tier_base_security_status": base_security_status,
    }


class TursoGemHunterStore:
    """Gem Hunter tables on the repository's shared Turso DB-API adapter."""

    def __init__(self, connection: Any = None):
        if connection is None:
            backend = storage_backend()
            if backend != "TURSO_CLOUD_SQLITE":
                raise RuntimeError("Persistent Gem Hunter storage requires configured Turso Cloud.")
            connection = connect_database()
        self._db = connection

    def execute(self, sql: str, args: tuple[Any, ...] = ()):
        return self._db.execute(sql, args)

    def initialize(self) -> None:
        with self._db:
            self.execute("""CREATE TABLE IF NOT EXISTS gem_hunter_candidates (
                candidate_id TEXT PRIMARY KEY, chain TEXT NOT NULL, token_address TEXT NOT NULL,
                first_seen_utc TEXT NOT NULL, first_price_usd REAL, latest_price_usd REAL,
                latest_liquidity_usd REAL, candidate_status TEXT NOT NULL,
                evidence_json TEXT NOT NULL, updated_at_utc TEXT NOT NULL
            )""")
            self.execute("""CREATE TABLE IF NOT EXISTS gem_hunter_lifecycle_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT UNIQUE,
                candidate_id TEXT NOT NULL, event_type TEXT NOT NULL,
                event_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL
            )""")
            self.execute("CREATE INDEX IF NOT EXISTS idx_gem_hunter_events_candidate ON gem_hunter_lifecycle_events(candidate_id, event_at_utc)")

    def upsert_candidate(self, candidate: Mapping[str, Any]) -> None:
        chain = str(candidate.get("chain") or "").lower()
        address = str(candidate.get("token_address") or "")
        if not chain or not address:
            raise ValueError("candidate chain and token_address are required")
        candidate_id = f"{chain}:{address if chain == 'solana' else address.lower()}"
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        price = _finite(candidate.get("price_usd"))
        liquidity = _finite(candidate.get("liquidity_usd"))
        status = str(candidate.get("candidate_status") or "DISCOVERED")
        evidence = json.dumps(dict(candidate), ensure_ascii=False, sort_keys=True, default=str)
        with self._db:
            self.execute(
                """INSERT INTO gem_hunter_candidates
                (candidate_id, chain, token_address, first_seen_utc, first_price_usd,
                 latest_price_usd, latest_liquidity_usd, candidate_status, evidence_json, updated_at_utc)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(candidate_id) DO UPDATE SET
                latest_price_usd=excluded.latest_price_usd,
                latest_liquidity_usd=excluded.latest_liquidity_usd,
                candidate_status=excluded.candidate_status,
                evidence_json=excluded.evidence_json, updated_at_utc=excluded.updated_at_utc""",
                (candidate_id, chain, address, now, price, price, liquidity, status, evidence, now),
            )

    def append_event(self, candidate_id: str, event_type: str, payload: Mapping[str, Any],
                     event_key: Optional[str] = None) -> bool:
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with self._db:
            cursor = self.execute(
                """INSERT OR IGNORE INTO gem_hunter_lifecycle_events
                (event_key, candidate_id, event_type, event_at_utc, payload_json)
                VALUES (?, ?, ?, ?, ?)""",
                (event_key, candidate_id, event_type, now,
                 json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, default=str)),
            )
            affected = getattr(cursor, "rowcount", None)
            if affected is None or int(affected) < 0:
                raise RuntimeError("Turso did not confirm rows_affected; refusing to assume event was stored")
            return int(affected) == 1

    def release_event(self, event_key: str) -> None:
        """Release a failed, unsent alert reservation so a later scan can retry."""
        with self._db:
            self.execute(
                "DELETE FROM gem_hunter_lifecycle_events WHERE event_key=? AND event_type='TELEGRAM_ALERT_RESERVED'",
                (event_key,),
            )

    def close(self) -> None:
        self._db.close()


def persist_candidate_lifecycle(store: Any, candidate: Mapping[str, Any],
                                previous_status: Optional[str] = None) -> str:
    chain = str(candidate.get("chain") or "").lower()
    address = str(candidate.get("token_address") or "")
    if not chain or not address:
        raise ValueError("candidate chain and token_address are required")
    candidate_id = f"{chain}:{address if chain == 'solana' else address.lower()}"
    store.upsert_candidate(candidate)
    status = str(candidate.get("candidate_status") or "DISCOVERED")
    store.append_event(candidate_id, "CANDIDATE_SNAPSHOT", dict(candidate))
    if previous_status != status:
        store.append_event(candidate_id, "STATUS_CHANGED", {
            "previous_status": previous_status, "new_status": status,
        })
    return candidate_id


def notify_qualified_gem(candidate: Mapping[str, Any], store: Any, *,
                         session: Any = None, bot_token: Optional[str] = None,
                         chat_id: Optional[str] = None, timeout: float = 10.0) -> dict[str, Any]:
    """Send a structured Persian VIP research card; never a trade instruction."""
    status = candidate.get("gem_hunter_v3_status", candidate.get("candidate_status"))
    allowed = {"TIER_1_VERIFIED_GEM", "TIER_2_EARLY_ALPHA", "EARLY_ALPHA_REVIEW"}
    if status not in allowed or candidate.get("gem_hunter_v3_trade_authorization", "NO_TRADE") != "NO_TRADE":
        return {"status": "SKIPPED", "reason": "Candidate is not in an eligible Telegram research tier."}

    security = candidate.get("security_evidence") or {}
    exit_gate = candidate.get("exit_slippage_evidence") or {}
    market = candidate.get("market_reconciliation") or {}
    if status == "TIER_1_VERIFIED_GEM" and security.get("status") != "PASS":
        return {"status": "BLOCKED", "reason": "Tier 1 requires complete security PASS."}
    if status == "TIER_2_EARLY_ALPHA" and security.get("status") != "PASS" and candidate.get("tier_base_security_status") != "PASS":
        return {"status": "BLOCKED", "reason": "Tier 2 requires explicit base anti-honeypot/sellability PASS."}
    if status == "TIER_1_VERIFIED_GEM" and (exit_gate.get("status") != "PASS" or market.get("status") != "PASS"):
        return {"status": "BLOCKED", "reason": "Tier 1 requires confirmed market reconciliation and exit gate."}
    if status == "EARLY_ALPHA_REVIEW":
        price_check = _finite(candidate.get("price_usd"))
        liquidity_check = _finite(
            market.get("verified_liquidity_usd")
            if market.get("status") == "PASS" else candidate.get("liquidity_usd")
        )
        volume_check = _finite(
            market.get("verified_volume_24h_usd")
            if market.get("status") == "PASS" else candidate.get("volume_24h_usd")
        )
        hard_block = (
            security.get("status") == "FAIL"
            or candidate.get("tier_base_security_status") == "FAIL"
            or security.get("honeypot_detected") is True
            or security.get("transfer_simulation_status") == "FAIL"
            or (_finite(security.get("sell_tax_pct")) is not None
                and _finite(security.get("sell_tax_pct")) > 5.0)
        )
        if hard_block:
            return {"status": "BLOCKED", "reason": "Explicit security/sellability failure blocks EARLY_ALPHA_REVIEW alerts."}
        if not (price_check is not None and 0 < price_check < 0.05
                and liquidity_check is not None and liquidity_check >= 50_000
                and volume_check is not None and volume_check >= 500_000):
            return {"status": "BLOCKED", "reason": "EARLY_ALPHA_REVIEW alert requires price < $0.05, liquidity >= $50,000 and 24h volume >= $500,000."}

    chain = str(candidate.get("chain") or "").lower()
    address = str(candidate.get("token_address") or "")
    if not address:
        return {"status": "BLOCKED", "reason": "Token contract address is missing."}
    candidate_id = f"{chain}:{address if chain == 'solana' else address.lower()}"
    six_hour_bucket = int(datetime.now(timezone.utc).timestamp() // (6 * 60 * 60))
    event_key = f"TELEGRAM_VIP_CARD_V1:{status}:{candidate_id}:{six_hour_bucket}"
    token = bot_token or os.getenv("GEM_HUNTER_TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
    destination = chat_id or os.getenv("GEM_HUNTER_TELEGRAM_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID")
    if not token or not destination:
        return {"status": "NOT_CONFIGURED", "reason": "Telegram bot token or chat id is missing."}
    inserted = store.append_event(candidate_id, "TELEGRAM_ALERT_RESERVED", {
        "candidate_status": status, "trade_authorization": "NO_TRADE",
        "dedupe_window_hours": 6, "dedupe_bucket_utc": six_hour_bucket,
    }, event_key=event_key)
    if not inserted:
        return {"status": "DUPLICATE_SUPPRESSED", "candidate_id": candidate_id}

    momentum = _finite(candidate.get("short_term_momentum_score",
                                     candidate.get("short_term_momentum_smart_money_score")))
    whale_flow = candidate.get("pool_trade_evidence") or {}
    whale_ratio = whale_flow.get("whale_buy_sell_ratio_1h")
    whale_events = whale_flow.get("whale_events") if isinstance(whale_flow.get("whale_events"), list) else []
    price = _finite(candidate.get("price_usd"))
    liquidity = _finite(market.get("verified_liquidity_usd")
                        if market.get("status") == "PASS" else candidate.get("liquidity_usd"))
    volume_24h = _finite(market.get("verified_volume_24h_usd")
                         if market.get("status") == "PASS" else candidate.get("volume_24h_usd"))
    estimated_tokens = candidate.get("estimated_tokens_for_100_usd")
    if estimated_tokens is None and price is not None and price > 0:
        estimated_tokens = int(100.0 / price)

    token_name = html_escape(str(candidate.get("token_name") or "نامشخص"))
    token_symbol = html_escape(str(candidate.get("token_symbol") or "?"))
    safe_address = html_escape(address)
    network_names = {
        "solana": "Solana (شبکه سولانا)",
        "base": "Base (شبکه بیس)",
        "ethereum": "Ethereum (شبکه اتریوم)",
    }
    network_label = network_names.get(chain, f"{html_escape(chain or 'نامشخص')} (شبکه نامشخص)")
    tier_title = {
        "TIER_1_VERIFIED_GEM": "⭐ ستاره طلایی TIER 1",
        "TIER_2_EARLY_ALPHA": "🔥 فرصت اولیه TIER 2",
        "EARLY_ALPHA_REVIEW": "🟠 فرصت زودهنگام نیازمند بررسی",
    }[status]
    price_text = f"{price:.10g}" if price is not None else "نامشخص"
    liquidity_text = f"{liquidity:,.2f}" if liquidity is not None else "نامشخص"
    volume_text = f"{volume_24h:,.2f}" if volume_24h is not None else "نامشخص"
    tokens_text = f"{int(estimated_tokens):,}" if _finite(estimated_tokens) is not None else "نامشخص"

    market_source = str(market.get("provider") or market.get("source") or "GeckoTerminal")
    security_provider = str(security.get("provider") or "نامشخص")
    if security_provider == "GOPLUS_SOURCIFY":
        security_source = "GoPlus / Sourcify"
    elif security_provider == "SOLANA_RPC":
        security_source = "Solana RPC"
    else:
        security_source = security_provider
    strengths = candidate.get("tier_strengths_fa") or []
    cautions = candidate.get("tier_cautions_fa") or []
    strength_lines = [
        f"• {html_escape(str(item))} <i>[مرجع: {html_escape(market_source)}]</i>"
        for item in strengths[:5]
    ] or [f"• داده‌های اولیه بازار ثبت شده‌اند. <i>[مرجع: {html_escape(market_source)}]</i>"]
    caution_lines = [
        f"• {html_escape(str(item))} <i>[مرجع: {html_escape(security_source)}]</i>"
        for item in cautions[:5]
    ]
    if security.get("status") != "PASS":
        caution_lines.append(
            f"• امنیت قرارداد تأیید کامل نشده است؛ وضعیت {html_escape(str(security.get('status') or 'UNKNOWN'))} را سالم تلقی نکن. <i>[مرجع: {html_escape(security_source)}]</i>"
        )
    if not caution_lines:
        caution_lines.append(f"• حتی عبور از کنترل‌های موجود، ممیزی کامل قرارداد یا تضمین امکان فروش نیست. <i>[مرجع: {html_escape(security_source)}]</i>")

    market_cap = _finite(candidate.get("market_cap_usd", candidate.get("fdv_usd")))
    market_cap_label = "ارزش بازار" if _finite(candidate.get("market_cap_usd")) is not None else "FDV"
    if market_cap is not None and market_cap > 0:
        scenario_text = (
            f"ارزش فعلی {market_cap_label}: ${market_cap:,.0f}. سناریوی فرضی ۲x یعنی "
            f"${market_cap * 2:,.0f} و ۵x یعنی ${market_cap * 5:,.0f}؛ این‌ها محاسبات سناریویی‌اند، نه هدف قیمت یا پیش‌بینی."
        )
    else:
        scenario_text = "ارزش بازار/FDV معتبر در Snapshot موجود نیست؛ بنابراین سناریوی ۲x تا ۵x محاسبه نمی‌شود و عددی حدس زده نخواهد شد."
    if momentum is not None and momentum >= 70:
        horizon_text = "با توجه به شتاب مشاهده‌شده، افق پیگیری پژوهشی ۳ تا ۱۴ روزه قابل بررسی است؛ نه توصیه به نگهداری."
    elif momentum is not None and momentum >= 50:
        horizon_text = "شتاب متوسط است؛ فقط تا روشن‌شدن روند و حجم، در واچ‌لیست کوتاه‌مدت بماند."
    else:
        horizon_text = "شتاب کافی یا داده معتبر برای تعیین افق نگهداری وجود ندارد؛ فعلاً فقط پایش داده‌ای."
    scenario_text += " " + horizon_text + " در صورت شکست حمایت، افت حجم یا بدترشدن امکان خروج، سناریوی رشد باطل می‌شود. این برآورد پژوهشی، غیرقطعی و بدون تضمین سود است."

    if chain == "solana":
        dex_url = f"https://dexscreener.com/solana/{address}"
        trade_url = f"https://jup.ag/swap/SOL-{address}"
        wallet_guide = (
            "در Trust Wallet یا Phantom بخش Swap/Browser را باز کن، شبکه Solana را انتخاب کن، "
            "آدرس قرارداد بالا را خودت تطبیق بده و فقط در صورت پذیرش ریسک با SOL یا USDC تبدیل کن."
        )
        trade_label = "بازکردن توکن در Jupiter"
    elif chain in {"base", "ethereum"}:
        dex_url = f"https://dexscreener.com/{chain}/{address}"
        trade_url = f"https://app.uniswap.org/#/swap?chain={chain}&inputCurrency=ETH&outputCurrency={address}"
        wallet_guide = (
            f"در Trust Wallet یا MetaMask شبکه {network_label} را دستی انتخاب کن، "
            "آدرس قرارداد را با منبع مستقل تطبیق بده و سپس در Swap آن را به‌عنوان توکن مقصد وارد کن. "
            "شبکه اشتباه می‌تواند باعث از دست‌رفتن دارایی شود."
        )
        trade_label = "بازکردن توکن در Uniswap"
    else:
        dex_url = "https://dexscreener.com/"
        trade_url = ""
        wallet_guide = "برای این شبکه لینک Swap خودکار ارائه نمی‌شود؛ ابتدا شبکه و قرارداد را از منبع رسمی تأیید کن."
        trade_label = ""

    dex_link = f'<a href="{html_escape(dex_url, quote=True)}">مشاهده نمودار و جفت‌ارز در DEX Screener</a>'
    trade_link = (
        f'<a href="{html_escape(trade_url, quote=True)}">{trade_label}</a>'
        if trade_url else "لینک خرید مستقیم ارائه نشده است."
    )
    dashboard_url = "https://crypto-super-radar-unlimited-demo-staging.onrender.com"
    dashboard_link = f'<a href="{dashboard_url}">📊 مشاهده گزارش کامل و ایردراپ‌ها در داشبورد CRYPTO SUPER RADAR</a>'

    text = (
        "🐋 <b>CRYPTO SUPER RADAR · رادار شکار فرصت‌ها</b>\n"
        f"{tier_title}\n"
        "\n"
        "🧾 <b>بخش ۱ | شناسنامه و اعداد کلیدی</b>\n"
        f"<b>نام و نماد ارز:</b> {token_name} ({token_symbol})\n"
        f"<b>بلاک‌چین / شبکه:</b> {network_label}\n"
        f"<b>قیمت فعلی:</b> ${price_text}\n"
        f"<b>مقدار قابل خرید با ۱۰۰ دلار:</b> حدود {tokens_text} توکن (تخمینی؛ بدون کارمزد و لغزش)\n"
        f"<b>نقدینگی استخر:</b> ${liquidity_text} | <b>حجم ۲۴ ساعته:</b> ${volume_text}\n"
        f"<b>آدرس قرارداد (برای کپی):</b> <code>{safe_address}</code>\n"
        "\n"
        "🐋 <b>جریان نهنگ‌ها در یک ساعت اخیر</b>\n"
        f"{html_escape(str(whale_flow.get('whale_net_action_fa') or 'دادهٔ معتبر نهنگ در دسترس نیست'))}\n"
        f"<b>حجم خرید / فروش نهنگ‌ها:</b> ${_finite(whale_flow.get('whale_buy_volume_1h_usd')) or 0:,.2f} / ${_finite(whale_flow.get('whale_sell_volume_1h_usd')) or 0:,.2f} · نسبت خرید به فروش: {whale_ratio if whale_ratio is not None else 'نامشخص'}\n"
        + "\n".join([f"• {html_escape(str(e.get('wallet_address') or 'کیف‌پول نامشخص'))} · {html_escape(str(e.get('event_type') or 'SWAP'))} · ${_finite(e.get('amount_usd')) or 0:,.2f} · تعداد توکن: {e.get('token_amount') if e.get('token_amount') is not None else 'ناموجود'} · {html_escape(str(e.get('timestamp_utc') or 'زمان نامشخص'))} · <a href=\"{html_escape(str(e.get('explorer_tx_url') or ''), quote=True)}\">تراکنش</a>" for e in whale_events[:8]]) + "\n"
        "\n"
        "🧠 <b>بخش ۲ | تحلیل هوشمند، نقاط قوت، ضعف و رفرنس</b>\n"
        f"<b>شتاب مشاهده‌شده:</b> {momentum if momentum is not None else 'نامشخص'} از ۱۰۰\n"
        "<b>نقاط قوت:</b>\n"
        + "\n".join(strength_lines) + "\n"
        "<b>نقاط ضعف و هشدارها:</b>\n"
        + "\n".join(caution_lines) + "\n"
        f"<b>سناریوی رشد و افق پیگیری (پژوهشی):</b> {html_escape(scenario_text)}\n"
        "\n"
        "🛡️ <b>بخش ۳ | راهنمای امن استفاده از کیف پول</b>\n"
        f"{trade_link}\n"
        f"{dex_link}\n"
        f"{html_escape(wallet_guide)}\n"
        "⚠️ قبل از Swap، آدرس قرارداد و شبکه را دوباره بررسی کن؛ از لینک تبلیغاتی، توکن هم‌نام و پیام خصوصی افراد ناشناس استفاده نکن. Seed Phrase یا کلید خصوصی را هرگز وارد نکن.\n"
        "\n"
        "📡 <b>بخش ۴ | داشبورد زنده</b>\n"
        f"{dashboard_link}\n"
        "\n"
        "<i>وضعیت سیستم: پژوهشی · NO_TRADE (بدون معامله خودکار)</i>"
    )
    client = session or requests
    try:
        response = client.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": destination, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError("Telegram API did not confirm the message")
    except Exception:
        store.release_event(event_key)
        raise
    return {"status": "SENT", "candidate_id": candidate_id,
            "message_id": (payload.get("result") or {}).get("message_id")}




def process_gem_hunter_v3_candidates(candidates: Any, *, notify: bool = True) -> list[dict[str, Any]]:
    """Enrich a discovery batch, persist its lifecycle when Turso is configured,
    and send deduplicated research alerts. This path never authorizes a trade.
    """
    rows = [dict(row) for row in candidates if isinstance(row, Mapping)] if isinstance(candidates, list) else []
    store = None
    storage_status = "NOT_CONFIGURED"
    try:
        if storage_backend() == "TURSO_CLOUD_SQLITE":
            store = TursoGemHunterStore()
            store.initialize()
            storage_status = "READY"
    except Exception as exc:
        store = None
        storage_status = f"UNKNOWN:{type(exc).__name__}"

    for row in rows:
        reconciliation = row.get("market_reconciliation") or {}
        verified_liquidity = (
            reconciliation.get("verified_liquidity_usd")
            if reconciliation.get("status") == "PASS" else None
        )
        existing_exit = row.get("exit_slippage_evidence") or {}
        existing_qualification = row.get("qualification") or {}
        existing_metrics = existing_qualification.get("market_metrics") or {}
        if _finite(existing_exit.get("estimated_slippage_bps")) is None and _finite(existing_metrics.get("estimated_slippage_bps")) is not None:
            existing_exit = {"status": "REPORTED", "estimated_slippage_bps": _finite(existing_metrics.get("estimated_slippage_bps")), "model": "QUALIFICATION_METRIC_PASSTHROUGH"}
        calculated_exit = estimate_exit_slippage(
            liquidity_usd=verified_liquidity,
            order_usd=_finite(os.getenv("GEM_HUNTER_EXIT_ORDER_USD")) or DEFAULT_EXIT_ORDER_USD,
        )
        # Retain a previously computed, numeric estimate rather than replacing it with UNKNOWN.
        row["exit_slippage_evidence"] = (
            existing_exit if _finite(existing_exit.get("estimated_slippage_bps")) is not None
            else calculated_exit
        )
        original_status = row.get("candidate_status")
        original_trade_authorization = row.get("trade_authorization")
        scores = score_candidate_v3(row)
        row.update(scores)
        tier = classify_gem_tier(row, scores)
        row.update(tier)
        row["gem_hunter_v3_status"] = tier.get("tier")
        row["candidate_status"] = original_status
        row["trade_authorization"] = original_trade_authorization
        row["gem_hunter_v3_trade_authorization"] = "NO_TRADE"
        metrics = {
            "holder_count": row.get("holder_count"),
            "unique_traders_24h": row.get("unique_traders_24h"),
            "unique_traders_24h_basis": row.get("unique_traders_24h_basis"),
            "buy_volume_share_pct": row.get("buy_volume_share_pct"),
            "estimated_slippage_bps": row.get("estimated_slippage_bps"),
        }
        row["opportunity_score"] = row.get("opportunity_score", row.get("short_term_momentum_score"))
        qualification = row.get("qualification")
        if isinstance(qualification, dict) and qualification.get("schema_version") == SCHEMA_VERSION:
            qualification = dict(qualification)
            market_metrics = dict(qualification.get("market_metrics") or {})
            market_metrics.update(metrics)
            qualification["market_metrics"] = market_metrics
            qualification["opportunity_score"] = row["opportunity_score"]
            qualification["risk_score"] = row.get("risk_score")
            row["qualification"] = qualification
        row["gem_hunter_storage_status"] = storage_status
        if store is not None:
            try:
                persist_candidate_lifecycle(store, row)
                row["gem_hunter_storage_status"] = "PERSISTED"
            except Exception as exc:
                row["gem_hunter_storage_status"] = f"UNKNOWN:{type(exc).__name__}"
        if notify and store is not None and row.get("gem_hunter_v3_status") in {"TIER_1_VERIFIED_GEM", "TIER_2_EARLY_ALPHA", "EARLY_ALPHA_REVIEW"}:
            try:
                row["telegram_alert_status"] = notify_qualified_gem(row, store)
            except Exception as exc:
                row["telegram_alert_status"] = {"status": "ERROR", "reason": type(exc).__name__}
        else:
            row["telegram_alert_status"] = {"status": "NOT_SENT"}
    if store is not None:
        store.close()
    return rows
