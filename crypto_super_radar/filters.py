"""Nine-stage mandatory gate evaluation and score components."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

import pandas as pd

from .config import AppConfig
from .indicators import latest_candle_age_hours, market_regime, technical_metrics
from .models import AssetSpec, GateResult, GateStatus, MarketSnapshot
from .sentiment import score_headlines


def gate(gate_id: int, en: str, fa: str, status: GateStatus, reason_en: str, reason_fa: str, **evidence: Any) -> GateResult:
    return GateResult(gate_id, en, fa, status, reason_en, reason_fa, evidence)


def evaluate_filters(
    asset: AssetSpec,
    snapshot: MarketSnapshot,
    candles: pd.DataFrame,
    benchmark: pd.DataFrame,
    security: Dict[str, Any],
    headlines: list[str],
    config: AppConfig,
) -> Dict[str, Any]:
    thresholds = config.thresholds
    gates = []

    # Gate 1 — data integrity and freshness.
    age = latest_candle_age_hours(candles)
    pool_age_days = None
    pool_timestamp_known = True
    if asset.market_type == "dex":
        pool_created = pd.to_datetime(snapshot.pool_created_at, utc=True, errors="coerce")
        if pd.isna(pool_created):
            pool_timestamp_known = False
        else:
            pool_age_days = max(0.0, float((pd.Timestamp.now(tz="UTC") - pool_created).total_seconds() / 86400.0))
    if age is None or not pool_timestamp_known:
        g1_status = GateStatus.UNKNOWN
        g1_en = "Candle freshness or DEX pool creation timestamp is missing/unreadable."
        g1_fa = "زمان کندل یا زمان ایجاد استخر DEX ناموجود/ناخواناست؛ استخر تازه یا داده صفر فرض نمی‌شود."
    elif age > thresholds.max_candle_age_hours:
        g1_status = GateStatus.FAIL
        g1_en = f"Latest candle is stale ({age:.1f} hours old)."
        g1_fa = f"آخرین کندل قدیمی است ({age:.1f} ساعت)؛ معامله متوقف شد."
    else:
        g1_status = GateStatus.PASS
        g1_en = f"Two providers agree within {thresholds.provider_discrepancy_pct:.1f}% and candles are fresh ({age:.1f} hours)."
        g1_fa = f"دو منبع قیمت و حجم را تأیید کرده‌اند و کندل‌ها تازه‌اند ({age:.1f} ساعت)."
    gates.append(gate(1, "Data integrity", "اعتبار داده", g1_status, g1_en, g1_fa,
                      price_discrepancy_pct=snapshot.price_discrepancy_pct,
                      volume_discrepancy_pct=snapshot.volume_discrepancy_pct,
                      candle_age_hours=age, pool_age_days=pool_age_days, provider_a=snapshot.provider_a, provider_b=snapshot.provider_b))

    # Gate 2 — market regime.
    regime = market_regime(benchmark)
    if regime["regime"] == "UNKNOWN":
        g2_status = GateStatus.UNKNOWN
        g2_en = "BTC benchmark history is insufficient."
        g2_fa = "تاریخچه کافی برای تشخیص وضعیت کلی بازار وجود ندارد."
    elif regime["regime"] == "NEUTRAL":
        g2_status = GateStatus.FAIL
        g2_en = "Broad market regime is neutral; no directional edge is accepted."
        g2_fa = "بازار کلی خنثی است؛ در این وضعیت جهت معاملاتی تأیید نمی‌شود."
    else:
        g2_status = GateStatus.PASS
        g2_en = f"BTC benchmark regime is {regime['regime']}."
        g2_fa = "روند کلی بازار با داده BTC مشخص شده است: " + ("صعودی." if regime["regime"] == "BULLISH" else "نزولی.")
    gates.append(gate(2, "Market regime", "وضعیت کلی بازار", g2_status, g2_en, g2_fa, **regime))

    # Technical metrics needed by later gates.
    tech = technical_metrics(candles, benchmark)
    if tech.get("status") != "KNOWN":
        tech_direction = "NONE"
    else:
        tech_direction = tech["direction"]

    # Gate 3 — trend/structure.
    aligned = (
        (regime["regime"] == "BULLISH" and tech_direction == "LONG")
        or (regime["regime"] == "BEARISH" and tech_direction == "SHORT")
    )
    if tech.get("status") != "KNOWN":
        g3_status, g3_en, g3_fa = GateStatus.UNKNOWN, tech.get("reason", "Unknown technical data."), "اطلاعات کافی برای محاسبه ساختار روند وجود ندارد."
    elif aligned:
        g3_status, g3_en, g3_fa = GateStatus.PASS, f"EMA20/50/200 structure aligns with {tech_direction}.", "ترتیب میانگین‌های متحرک با جهت بازار همسو است."
    else:
        g3_status, g3_en, g3_fa = GateStatus.FAIL, "Asset trend structure does not align with the broad market.", "ساختار روند دارایی با جهت بازار کلی هم‌سو نیست."
    tech_evidence = {k: v for k, v in tech.items() if k != "status"}
    tech_evidence["technical_data_status"] = tech.get("status")
    gates.append(gate(3, "Trend and structure", "روند و ساختار قیمت", g3_status, g3_en, g3_fa, **tech_evidence))

    # Gate 4 — RSI and relative strength.
    if tech.get("status") != "KNOWN":
        g4_status, g4_en, g4_fa = GateStatus.UNKNOWN, "Momentum metrics unavailable.", "شاخص‌های مومنتوم قابل محاسبه نیستند."
    elif tech_direction == "LONG":
        ok = 50.0 <= tech["rsi14"] <= 70.0 and tech["relative_strength_30d_pct"] > thresholds.min_relative_strength_30d_pct
        g4_status = GateStatus.PASS if ok else GateStatus.FAIL
        g4_en = "RSI and 30-day relative strength confirm the long direction." if ok else "RSI or relative strength fails the long threshold."
        g4_fa = "قدرت حرکت و عملکرد نسبی دارایی خرید را تأیید می‌کنند." if ok else "قدرت حرکت یا قدرت نسبی برای خرید کافی نیست."
    elif tech_direction == "SHORT":
        ok = 30.0 <= tech["rsi14"] <= 50.0 and tech["relative_strength_30d_pct"] < 0
        g4_status = GateStatus.PASS if ok else GateStatus.FAIL
        g4_en = "RSI and relative strength confirm the short direction." if ok else "RSI or relative strength fails the short threshold."
        g4_fa = "مومنتوم و عملکرد نسبی جهت نزولی را تأیید می‌کنند." if ok else "قدرت حرکت یا قدرت نسبی برای فروش استقراضی تأیید نمی‌شود."
    else:
        g4_status, g4_en, g4_fa = GateStatus.FAIL, "No clean directional setup.", "جهت معاملاتی روشن و قابل اتکا وجود ندارد."
    gates.append(gate(4, "Momentum and relative strength", "مومنتوم و قدرت نسبی", g4_status, g4_en, g4_fa,
                      rsi14=tech.get("rsi14"), relative_strength_30d_pct=tech.get("relative_strength_30d_pct")))

    # Gate 5 — capital flow and liquidity.
    turnover = None
    if asset.market_type == "dex":
        if snapshot.liquidity_usd is None or snapshot.liquidity_usd <= 0:
            g5_status, g5_en, g5_fa = GateStatus.UNKNOWN, "DEX liquidity is missing.", "عمق نقدینگی DEX مشخص نیست."
        else:
            turnover = snapshot.volume_24h_usd / snapshot.liquidity_usd
            ok = (snapshot.liquidity_usd >= thresholds.min_dex_liquidity_usd
                  and snapshot.volume_24h_usd >= thresholds.min_dex_volume_24h_usd
                  and turnover <= thresholds.max_volume_to_liquidity)
            g5_status = GateStatus.PASS if ok else GateStatus.FAIL
            g5_en = "DEX liquidity, volume and turnover pass screening." if ok else "DEX liquidity/volume is too low or turnover is abnormal."
            g5_fa = "عمق نقدینگی و حجم معاملات در محدوده قابل قبول است." if ok else "نقدینگی یا حجم کافی نیست، یا گردش حجم غیرعادی است."
    elif asset.market_type == "perp":
        oi_a = snapshot.raw.get("open_interest_a")
        oi_b = snapshot.raw.get("open_interest_b")
        funding = snapshot.raw.get("funding_rate_8h")
        if oi_a is None or oi_b is None or funding is None:
            g5_status, g5_en, g5_fa = GateStatus.UNKNOWN, "Open interest or funding is unavailable.", "داده موقعیت‌های باز یا Funding در دسترس نیست."
        else:
            oi_ok = oi_a > 0 and oi_b > 0
            funding_ok = not ((tech_direction == "LONG" and funding > 0.0005) or (tech_direction == "SHORT" and funding < -0.0005))
            ok = snapshot.volume_24h_usd >= thresholds.min_cex_quote_volume_24h_usd and oi_ok and funding_ok
            g5_status = GateStatus.PASS if ok else GateStatus.FAIL
            g5_en = "Perpetual volume, open interest and funding crowding pass screening." if ok else "Perpetual liquidity, open interest or funding crowding fails screening."
            g5_fa = "حجم قرارداد، موقعیت‌های باز و Funding برای جهت معامله قابل قبول‌اند." if ok else "حجم، موقعیت‌های باز یا ازدحام Funding با جهت معامله سازگار نیست."
    else:
        ok = snapshot.volume_24h_usd >= thresholds.min_cex_quote_volume_24h_usd
        g5_status = GateStatus.PASS if ok else GateStatus.FAIL
        g5_en = "CEX quote turnover passes the minimum; order-book depth is not available in this prototype." if ok else "CEX quote turnover is below the minimum."
        g5_fa = "حجم دلاری معاملات صرافی از حداقل لازم بالاتر است؛ عمق دفتر سفارش هنوز مستقیماً بررسی نشده." if ok else "حجم دلاری معاملات صرافی کمتر از حداقل تعیین‌شده است."
    gates.append(gate(5, "Capital flow and liquidity", "جریان سرمایه و نقدینگی", g5_status, g5_en, g5_fa,
                      volume_24h_usd=snapshot.volume_24h_usd, liquidity_usd=snapshot.liquidity_usd, volume_to_liquidity=turnover))

    # Gate 6 — token/contract security.
    sec_status_text = security.get("status")
    if sec_status_text == "PASS":
        g6_status = GateStatus.PASS
        g6_en = security.get("reason", "Minimum asset security checks passed.")
        g6_fa = "حداقل بررسی‌های امنیت دارایی با موفقیت گذرانده شده‌اند؛ این تأیید به معنی بی‌ریسک‌بودن نیست."
    elif sec_status_text == "FAIL":
        g6_status = GateStatus.FAIL
        g6_en = security.get("reason", "Security checks failed.")
        g6_fa = "یکی از بررسی‌های امنیتی رد شده است؛ معامله ممنوع است."
    elif sec_status_text == "KNOWN":
        active = bool(security.get("mint_authority_active") or security.get("freeze_authority_active"))
        concentration = security.get("top10_token_account_concentration_pct")
        concentration_bad = concentration is not None and concentration > thresholds.max_sol_top10_account_concentration_pct
        if active or concentration_bad:
            g6_status = GateStatus.FAIL
            g6_en = "Active mint/freeze authority or excessive top-token-account concentration detected."
            g6_fa = "اختیار ساخت/فریز فعال یا تمرکز بالای حساب‌های بزرگ توکن شناسایی شد."
        elif concentration is None:
            g6_status = GateStatus.UNKNOWN
            g6_en = "Top token-account concentration could not be calculated."
            g6_fa = "تمرکز حساب‌های بزرگ توکن محاسبه نشده؛ امنیت کامل تأیید نمی‌شود."
        else:
            g6_status = GateStatus.PASS
            g6_en = "Mint/freeze authorities are revoked and top-account concentration is below the configured threshold."
            g6_fa = "اختیار ساخت و فریز غیرفعال است و تمرکز ۱۰ حساب بزرگ از حد تعیین‌شده کمتر است."
    else:
        g6_status = GateStatus.UNKNOWN
        g6_en = security.get("reason", "Security evidence is unavailable or incomplete.")
        g6_fa = "بررسی امنیتی ناقص یا ناموجود است؛ سیستم دارایی را امن فرض نمی‌کند."
    security_evidence = {k: v for k, v in security.items() if k != "status"}
    security_evidence["security_data_status"] = sec_status_text or "UNKNOWN"
    gates.append(gate(6, "On-chain and contract risk", "امنیت آن‌چین و قرارداد", g6_status, g6_en, g6_fa, **security_evidence))

    # Gate 7 — sentiment/event risk.
    sentiment = score_headlines(headlines)
    if sentiment["status"] == "UNKNOWN":
        g7_status = GateStatus.UNKNOWN
        g7_en = sentiment["reason"]
        g7_fa = "تیتر خبری معتبر وارد نشده است؛ احساسات بازار حدس زده نمی‌شود."
    else:
        adverse = sentiment["score"] < 35 if tech_direction == "LONG" else sentiment["score"] > 65
        overheated_hype = (sentiment.get("hype_score") or 0) >= 80 and tech.get("rsi14") is not None and tech["rsi14"] >= 68
        rejected = adverse or overheated_hype
        g7_status = GateStatus.FAIL if rejected else GateStatus.PASS
        g7_en = "No severe opposing sentiment or crowded-hype condition found." if not rejected else "Headlines show adverse sentiment or elevated hype alongside an overbought market."
        g7_fa = "خبر مخالف شدید یا هیجان بیش‌ازحد در کنار بازار داغ دیده نشد." if not rejected else "خبر مخالف شدید یا هیجان خبری بالا همراه با اشباع خرید مشاهده شد."
    sentiment_evidence = {k: v for k, v in sentiment.items() if k != "status"}
    sentiment_evidence["sentiment_data_status"] = sentiment.get("status")
    gates.append(gate(7, "News and sentiment", "اخبار و احساسات", g7_status, g7_en, g7_fa, **sentiment_evidence))

    return {
        "gates": gates,
        "regime": regime,
        "technical": tech,
        "sentiment": sentiment,
        "volume_to_liquidity": turnover,
        "security": security,
    }
