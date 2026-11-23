"""Pandas adapters for the optional Persian Streamlit dashboard.

These functions are deliberately independent of Streamlit so they can be
unit-tested in the base, zero-cost installation.
"""
from __future__ import annotations

import json
import math
from typing import Any

import pandas as pd

from .indicators import observed_price_ranges
from .models import AnalysisResult


def _status(gate: Any) -> str:
    value = getattr(gate.status, "value", gate.status)
    return str(value).upper()


def _as_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _gate_map(result: AnalysisResult) -> dict[int, Any]:
    return {int(gate.gate_id): gate for gate in result.gates}


def gates_dataframe(result: AnalysisResult) -> pd.DataFrame:
    """Return the twelve mandatory gates with Persian labels and raw evidence."""
    rows = []
    for gate in sorted(result.gates, key=lambda item: item.gate_id):
        rows.append({
            "شماره": gate.gate_id,
            "فیلتر": gate.name_fa,
            "وضعیت": _status(gate),
            "توضیح": gate.reason_fa,
            "دلیل فنی": gate.reason_en,
            "شواهد": _as_json(gate.evidence),
        })
    return pd.DataFrame(rows, columns=[
        "شماره", "فیلتر", "وضعیت", "توضیح", "دلیل فنی", "شواهد",
    ])


def data_errors_dataframe(result: AnalysisResult) -> pd.DataFrame:
    """Show only FAIL/UNKNOWN gates; never replace missing evidence with zero."""
    rows = []
    for gate in sorted(result.gates, key=lambda item: item.gate_id):
        state = _status(gate)
        if state in {"FAIL", "UNKNOWN"}:
            rows.append({
                "شماره فیلتر": gate.gate_id,
                "فیلتر": gate.name_fa,
                "وضعیت": state,
                "علت": gate.reason_fa,
                "جزئیات": _as_json(gate.evidence),
            })
    return pd.DataFrame(rows, columns=[
        "شماره فیلتر", "فیلتر", "وضعیت", "علت", "جزئیات",
    ])


def quantitative_dataframe(result: AnalysisResult) -> pd.DataFrame:
    """Flatten market, momentum, liquidity and backtest evidence into metrics."""
    gates = _gate_map(result)
    rows: list[dict[str, Any]] = []

    def add(section: str, label: str, value: Any, unit: str = "") -> None:
        rows.append({
            "بخش": section,
            "شاخص": label,
            "مقدار": value,
            "واحد": unit,
        })

    data = gates.get(1)
    if data:
        e = data.evidence
        add("سلامت داده", "اختلاف قیمت بین منابع", e.get("price_discrepancy_pct"), "%")
        add("سلامت داده", "اختلاف حجم بین منابع", e.get("volume_discrepancy_pct"), "%")
        add("سلامت داده", "سن آخرین کندل", e.get("candle_age_hours"), "ساعت")

    regime = gates.get(2)
    if regime:
        e = regime.evidence
        add("رژیم بازار", "وضعیت بازار", e.get("regime"))
        add("رژیم بازار", "بازده ۳۰ روزه معیار", e.get("return_30d_pct"), "%")
        add("رژیم بازار", "EMA50 معیار", e.get("ema50"), "USD")
        add("رژیم بازار", "EMA200 معیار", e.get("ema200"), "USD")

    trend = gates.get(3)
    if trend:
        e = trend.evidence
        add("روند و مومنتوم", "قیمت آخر", e.get("close"), "USD")
        add("روند و مومنتوم", "RSI-14", e.get("rsi14"), "۰ تا ۱۰۰")
        add("روند و مومنتوم", "RSI-14 نسبت به کندل قبلی", e.get("rsi14_previous"), "۰ تا ۱۰۰")
        add("روند و مومنتوم", "تغییر RSI-14", e.get("rsi14_change"), "واحد RSI")
        add("روند و مومنتوم", "EMA20", e.get("ema20"), "USD")
        add("روند و مومنتوم", "EMA50", e.get("ema50"), "USD")
        add("روند و مومنتوم", "EMA200", e.get("ema200"), "USD")
        add("روند و مومنتوم", "ATR-14", e.get("atr14"), "USD")
        add("روند و مومنتوم", "قدرت نسبی ۳۰ روزه نسبت به BTC", e.get("relative_strength_30d_pct"), "%")
        add("روند و مومنتوم", "جهت ساختار", e.get("direction"))
        add("روند و مومنتوم", "رژیم دارایی", e.get("asset_regime"))

    wave = (result.metrics or {}).get("elliott_wave") or {}
    add("ساختار موج", "وضعیت شمارش الیوت", wave.get("status"))
    add("ساختار موج", "سناریوی ساختاری", wave.get("pattern"))
    add("ساختار موج", "فاز موج", wave.get("phase"))
    add("ساختار موج", "تعداد موج‌های تأییدشده", wave.get("wave_count"))
    add("ساختار موج", "موج بعدی سناریویی", wave.get("next_wave"))
    add("ساختار موج", "سطح ابطال شمارش", wave.get("invalidation_price"), "USD")
    add("ساختار موج", "تعداد پیوت‌های تأییدشده", wave.get("confirmed_pivot_count"))
    add("ساختار موج", "معنای اطمینان", wave.get("confidence_semantics"))

    shape = (result.metrics or {}).get("chart_pattern") or {}
    add("الگوی هندسی", "وضعیت الگو", shape.get("status"))
    add("الگوی هندسی", "نوع الگو", shape.get("pattern"))
    add("الگوی هندسی", "وضعیت شکست", shape.get("breakout"))
    add("الگوی هندسی", "تأیید شکست با حجم", shape.get("breakout_confirmation"))
    add("الگوی هندسی", "حجم شکست نسبت به میانهٔ ۲۰ کندل", shape.get("breakout_volume_ratio"), "برابر")
    add("الگوی هندسی", "تعداد برخورد سقف/کف", f"{shape.get('upper_touches', 0)}/{shape.get('lower_touches', 0)}")
    add("الگوی هندسی", "مرز بالایی فعلی", shape.get("upper_now"), "USD")
    add("الگوی هندسی", "مرز پایینی فعلی", shape.get("lower_now"), "USD")
    add("الگوی هندسی", "معنای اطمینان", shape.get("confidence_semantics"))

    liquidity = gates.get(5)
    if liquidity:
        e = liquidity.evidence
        add("جریان سرمایه و نقدینگی", "حجم ۲۴ ساعته", e.get("volume_24h_usd"), "USD")
        add("جریان سرمایه و نقدینگی", "نقدینگی", e.get("liquidity_usd"), "USD")
        add("جریان سرمایه و نقدینگی", "نسبت حجم به نقدینگی", e.get("volume_to_liquidity"), "نسبت")

    initial_oos = gates.get(8)
    if initial_oos:
        e = initial_oos.evidence
        add("بک‌تست اولیه", "Sharpe خارج از نمونه", e.get("oos_sharpe"))
        add("بک‌تست اولیه", "Profit Factor", e.get("oos_profit_factor"))
        add("بک‌تست اولیه", "Sortino خارج از نمونه", e.get("oos_sortino"))
        add("بک‌تست اولیه", "حداکثر افت سرمایه", e.get("oos_max_drawdown_pct"), "%")
        add("بک‌تست اولیه", "بازده خالص", e.get("oos_net_return_pct"), "%")
        add("بک‌تست اولیه", "تعداد معاملات/تغییر موقعیت", e.get("oos_trade_transitions"))
        add("بک‌تست اولیه", "تعداد کندل آزمون", e.get("oos_bars"))

    walk = gates.get(12)
    if walk:
        e = walk.evidence
        aggregate = e.get("aggregate_oos") or {}
        add("Walk-forward", "تعداد Fold", e.get("fold_count"))
        add("Walk-forward", "Sharpe تجمیعی آزمون", aggregate.get("sharpe"))
        add("Walk-forward", "Profit Factor تجمیعی آزمون", aggregate.get("profit_factor"))
        add("Walk-forward", "حداکثر Drawdown تجمیعی", aggregate.get("max_drawdown_pct"), "%")
        add("Walk-forward", "بدترین Drawdown بین Foldها", e.get("worst_fold_drawdown_pct"), "%")

    return pd.DataFrame(rows, columns=["بخش", "شاخص", "مقدار", "واحد"])


def whale_sentiment_dataframe(result: AnalysisResult) -> pd.DataFrame:
    """Show whale, sentiment and independent decision vectors in Persian."""
    metrics = result.metrics or {}
    gates = _gate_map(result)
    sentiment_gate = gates.get(11)
    sentiment_evidence = sentiment_gate.evidence if sentiment_gate else {}
    fear_greed = sentiment_evidence.get("fear_greed") or {}

    items = [
        ("هوش نهنگ‌ها", "وضعیت داده نهنگ", metrics.get("whale_status")),
        ("هوش نهنگ‌ها", "جهت فشار نهنگ", metrics.get("whale_bias")),
        ("هوش نهنگ‌ها", "تعداد مشاهده‌های تاریخی", metrics.get("whale_history_count")),
        ("هوش نهنگ‌ها", "جریان خالص صرافی", metrics.get("whale_exchange_net_flow_usd"), "USD"),
        ("هوش نهنگ‌ها", "انتقال خالص کیف‌پول‌های برچسب‌خورده", metrics.get("whale_wallet_net_transfer_usd"), "USD"),
        ("هوش نهنگ‌ها", "جریان معاملات بزرگ CEX", metrics.get("whale_cex_large_trade_net_usd"), "USD"),
        ("هم‌سویی شواهد", "هم‌سویی نهنگ با روند", metrics.get("whale_alignment")),
        ("هم‌سویی شواهد", "هم‌سویی احساسات با روند", metrics.get("sentiment_alignment")),
        ("احساسات بازار", "امتیاز احساسات", metrics.get("market_sentiment_score"), "از ۱۰۰- تا ۱۰۰"),
        ("احساسات بازار", "برچسب احساسات", metrics.get("market_sentiment_label")),
        ("احساسات بازار", "Fear & Greed", fear_greed.get("value"), "از ۰ تا ۱۰۰"),
        ("احساسات بازار", "دامنه Fear & Greed", fear_greed.get("scope")),
        ("احساسات بازار", "وضعیت خبر", sentiment_evidence.get("news_status")),
        ("احساسات بازار", "وضعیت منبع اجتماعی", sentiment_evidence.get("social_status")),
        ("امتیازهای مستقل", "Risk Score", result.estimated_risk_score, "۰ تا ۱۰۰؛ بیشتر یعنی ریسک بالاتر"),
        ("امتیازهای مستقل", "Opportunity Rank", result.opportunity_rank, "رتبه فرصت؛ احتمال سود نیست"),
        ("امتیازهای مستقل", "تعدیل رتبه بر اساس نهنگ", metrics.get("opportunity_whale_adjustment"), "امتیاز"),
        ("امتیازهای مستقل", "تعدیل رتبه بر اساس احساسات", metrics.get("opportunity_sentiment_adjustment"), "امتیاز"),
        ("امتیازهای مستقل", "تعدیل کل رتبه فرصت", metrics.get("opportunity_total_adjustment"), "امتیاز"),
        ("امتیازهای مستقل", "Confidence Level", metrics.get("confidence_level_pct"), "پوشش شواهد با جریمه واگرایی؛ احتمال سود نیست"),
        ("امتیازهای مستقل", "جریمه اطمینان به دلیل واگرایی", metrics.get("confidence_penalty_pct"), "واحد درصد"),
        ("امتیازهای مستقل", "پوشش خام شواهد", metrics.get("evidence_confidence_pct"), "%"),
    ]
    rows = []
    for item in items:
        section, label, value, *unit = item
        rows.append({
            "بخش": section,
            "شاخص": label,
            "مقدار": value,
            "واحد": unit[0] if unit else "",
        })
    return pd.DataFrame(rows, columns=["بخش", "شاخص", "مقدار", "واحد"])


def market_ranges_dataframe(candles: pd.DataFrame | None) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Return observed high/low ranges with explicit per-window coverage state."""
    evidence = observed_price_ranges(candles)
    rows = []
    labels = {"7": "کوتاه‌مدت · ۷ روز", "30": "میان‌مدت · ۳۰ روز",
              "90": "بلندمدت · ۹۰ روز", "365": "بلندمدت · ۳۶۵ روز"}
    for days, item in evidence.get("windows", {}).items():
        rows.append({
            "بازه": labels.get(days, f"{days} روز"),
            "کف مشاهده‌شده": item.get("low"),
            "سقف مشاهده‌شده": item.get("high"),
            "تعداد کندل": item.get("candle_count"),
            "پوشش داده": item.get("coverage_pct"),
            "وضعیت": item.get("status", "UNKNOWN"),
            "زمان ثبت کف": item.get("low_at"),
            "زمان ثبت سقف": item.get("high_at"),
            "علت": item.get("reason"),
        })
    return pd.DataFrame(rows), evidence


def build_dashboard_frames(
    result: AnalysisResult,
    candles: pd.DataFrame | None = None,
) -> dict[str, pd.DataFrame]:
    """Build all source-backed dashboard tables from one engine result."""
    ranges, _ = market_ranges_dataframe(candles)
    return {
        "gates": gates_dataframe(result),
        "errors": data_errors_dataframe(result),
        "quantitative": quantitative_dataframe(result),
        "whale_sentiment": whale_sentiment_dataframe(result),
        "market_ranges": ranges,
    }




_AIRDROP_RADAR_PROJECTS = [
    {
        "project": "Base Sepolia", "network": "Base", "free_level_fa": "کاملاً رایگان در محیط آزمایشی؛ توکن‌ها ارزش پولی ندارند.",
        "steps_fa": ["کیف‌پول سازگار با شبکه آزمایشی بسازید.", "شبکه Base Sepolia را انتخاب کنید.", "از Faucet رسمی مستندات Base توکن آزمایشی بگیرید.", "یک تراکنش آزمایشی انجام دهید؛ هیچ پاداشی تضمین نشده است."],
        "official_url": "https://docs.base.org/chain/faucet", "category": "TESTNET", "trade_authorization": "NO_TRADE",
    },
    {
        "project": "Optimism Testnets", "network": "Optimism", "free_level_fa": "تعامل آزمایشی رایگان؛ پاداش یا ایردراپ تضمین نیست.",
        "steps_fa": ["شبکه آزمایشی فعال را از مستندات رسمی بررسی کنید.", "توکن آزمایشی را فقط از Faucet معرفی‌شده در مستندات بگیرید.", "برنامه‌های آزمایشی رسمی را بررسی و تراکنش‌ها را ثبت کنید."],
        "official_url": "https://docs.optimism.io/chain/networks", "category": "TESTNET", "trade_authorization": "NO_TRADE",
    },
    {
        "project": "Arbitrum Sepolia", "network": "Arbitrum", "free_level_fa": "توکن تست‌نت رایگان و بدون ارزش معاملاتی؛ ایردراپ احتمالی تأیید نشده.",
        "steps_fa": ["شبکه Arbitrum Sepolia را طبق مستندات رسمی اضافه کنید.", "Faucetهای معتبر معرفی‌شده در مستندات را بررسی کنید.", "فقط برنامه‌های آزمایشی رسمی را اجرا کنید."],
        "official_url": "https://docs.arbitrum.io/", "category": "TESTNET", "trade_authorization": "NO_TRADE",
    },
    {
        "project": "Ethereum Sepolia", "network": "Ethereum", "free_level_fa": "توکن آزمایشی رایگان برای تست؛ فاقد ارزش واقعی.",
        "steps_fa": ["شبکه Sepolia را در کیف‌پول انتخاب کنید.", "از Faucet معرفی‌شده در مستندات Ethereum توکن آزمایشی بگیرید.", "قراردادها و تراکنش‌های آزمایشی را فقط در محیط تست اجرا کنید."],
        "official_url": "https://ethereum.org/en/developers/docs/networks/", "category": "TESTNET", "trade_authorization": "NO_TRADE",
    },
]


def airdrop_radar_projects() -> list[dict[str, Any]]:
    """Return curated official testnet entry points; never imply an airdrop is guaranteed."""
    return [dict(item, steps_fa=list(item["steps_fa"])) for item in _AIRDROP_RADAR_PROJECTS]


def airdrop_radar_dataframe() -> pd.DataFrame:
    """Build the Persian, research-only free-airdrop/testnet directory."""
    rows = []
    for item in airdrop_radar_projects():
        rows.append({
            "پروژه": item["project"], "شبکه": item["network"],
            "میزان رایگان بودن": item["free_level_fa"],
            "مراحل": "؛ ".join(item["steps_fa"]), "لینک رسمی": item["official_url"],
            "نوع": item["category"], "مجوز معامله": "NO_TRADE",
        })
    return pd.DataFrame(rows, columns=["پروژه", "شبکه", "میزان رایگان بودن", "مراحل", "لینک رسمی", "نوع", "مجوز معامله"])


def gem_hunter_v3_dataframe(candidates: Any) -> pd.DataFrame:
    """Build a mobile-friendly Gem Hunter V3 view without trade authorization."""
    rows = []
    for item in candidates if isinstance(candidates, list) else []:
        if not isinstance(item, dict):
            continue
        exit_gate = item.get("exit_slippage_evidence") or {}
        rows.append({
            "توکن": item.get("token_name") or item.get("token_symbol") or "نامشخص",
            "طبقهٔ کشف": item.get("tier_label_fa", item.get("tier", item.get("gem_hunter_v3_status", "RESEARCH_WATCH"))),
            "نقاط قوت": "؛ ".join(item.get("tier_strengths_fa") or []),
            "نکات احتیاط": "؛ ".join(item.get("tier_cautions_fa") or []),
            "اعتماد دادهٔ طبقه": item.get("tier_data_confidence", "LIMITED"),
            "امنیت پایه": item.get("tier_base_security_status", "UNKNOWN"),
            "شبکه": item.get("chain"),
            "قرارداد": item.get("token_address"),
            "قیمت": item.get("price_usd"),
            "نقدینگی تأییدشده": (item.get("market_reconciliation") or {}).get("verified_liquidity_usd") or item.get("liquidity_usd"),
            "حجم ۲۴ساعته": (item.get("market_reconciliation") or {}).get("verified_volume_24h_usd") or item.get("volume_24h_usd"),
            "حجم به نقدینگی": item.get("volume_to_liquidity_24h"),
            "امتیاز مومنتوم": item.get("short_term_momentum_score", item.get("short_term_momentum_smart_money_score")),
            "پوشش شواهد مومنتوم (%)": item.get("momentum_evidence_coverage_pct"),
            "پهنای خریداران مشاهده‌شده": item.get("wallet_breadth_evidence_status", "UNKNOWN"),
            "انتساب پول هوشمند": item.get("smart_money_evidence_status", "NOT_AVAILABLE"),
            "شناسایی خریداران جدید": item.get("new_buyer_cohort_status", "NOT_AVAILABLE"),
            "وضعیت هشدار تلگرام": (item.get("telegram_alert_status") or {}).get("status", "NOT_SENT"),
            "امتیاز ایمنی": item.get("safety_exit_liquidity_score"),
            "اعتماد داده": item.get("data_confidence_score"),
            "لغزش خروج (bps)": exit_gate.get("estimated_slippage_bps"),
            "تعداد هولدرها / حساب‌های بزرگ مشاهده‌شده": item.get("holder_count", (item.get("security_evidence") or {}).get("largest_accounts_count")),
            "معامله‌گران یکتا / برآورد تراکنش": item.get("unique_traders_24h"),
            "مبنای شمارش معامله‌گران": item.get("unique_traders_24h_basis", "NOT_AVAILABLE"),
            "سهم حجمی خرید (%)": item.get("buy_volume_share_pct"),
            "امتیاز فرصت (از ۱۰۰)": item.get("opportunity_score", item.get("short_term_momentum_score")),
            "امتیاز ریسک (از ۱۰۰)": item.get("risk_score"),
            "وضعیت خروج": exit_gate.get("status", "UNKNOWN"),
            "وضعیت Gem Hunter V3": item.get("gem_hunter_v3_status", "RESEARCH_WATCH"),
            "وضعیت ذخیره‌سازی": item.get("gem_hunter_storage_status", "UNKNOWN"),
            "مجوز معامله": "NO_TRADE",
        })
    return pd.DataFrame(rows, columns=[
        "توکن", "طبقهٔ کشف", "نقاط قوت", "نکات احتیاط", "اعتماد دادهٔ طبقه",
        "امنیت پایه", "شبکه", "قرارداد", "قیمت", "نقدینگی تأییدشده", "حجم ۲۴ساعته", "حجم به نقدینگی",
        "امتیاز مومنتوم", "پوشش شواهد مومنتوم (%)", "پهنای خریداران مشاهده‌شده",
        "انتساب پول هوشمند", "شناسایی خریداران جدید", "وضعیت هشدار تلگرام",
        "امتیاز ایمنی", "اعتماد داده", "لغزش خروج (bps)",
        "تعداد هولدرها / حساب‌های بزرگ مشاهده‌شده", "معامله‌گران یکتا / برآورد تراکنش", "مبنای شمارش معامله‌گران",
        "سهم حجمی خرید (%)", "امتیاز فرصت (از ۱۰۰)", "امتیاز ریسک (از ۱۰۰)",
        "وضعیت خروج", "وضعیت Gem Hunter V3", "وضعیت ذخیره‌سازی", "مجوز معامله",
    ])


def gem_hunter_v3_report_dataframe(report: Any) -> pd.DataFrame:
    """Convert the latest Turso worker report into the existing Persian V3 table."""
    if not isinstance(report, dict):
        return gem_hunter_v3_dataframe([])
    candidates = report.get("candidates")
    if not isinstance(candidates, list):
        candidates = report.get("gem_hunter_v3_candidates") or report.get("diagnostic_candidates") or []
    return gem_hunter_v3_dataframe(candidates)


def market_movement_label(price_change_24h_pct: Any) -> str:
    """Map a finite 24h percentage move to a concise Persian market state."""
    try:
        change = float(price_change_24h_pct)
    except (TypeError, ValueError, OverflowError):
        return "⚪ دادهٔ تغییر قیمت ناموجود"
    if not math.isfinite(change):
        return "⚪ دادهٔ تغییر قیمت ناموجود"
    if change > 5.0:
        return "🚀 در حال پامپ / صعود قوی"
    if change >= 1.0:
        return "🟢 متمایل به صعود"
    if change < -3.0:
        return "🔴 فشار فروش / نزولی"
    return "⚪ در حال تثبیت"


def market_board_dataframe(items: Any) -> pd.DataFrame:
    """Return a research-only market board ranked by 24h momentum, then turnover."""
    rows = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        change = item.get("price_change_24h_pct")
        try:
            change_sort = float(change)
            if not math.isfinite(change_sort):
                change_sort = float("-inf")
        except (TypeError, ValueError, OverflowError):
            change_sort = float("-inf")
        try:
            volume_sort = float(item.get("volume_24h_usd"))
            if not math.isfinite(volume_sort):
                volume_sort = -1.0
        except (TypeError, ValueError, OverflowError):
            volume_sort = -1.0
        rows.append({
            "نماد": item.get("symbol"),
            "تغییر ۲۴ساعته (%)": change,
            "حجم ۲۴ساعته (USD)": item.get("volume_24h_usd"),
            "جهت بازار": market_movement_label(change),
            "شتاب حجم (برابر)": item.get("volume_spike_ratio"),
            "وضعیت داده": item.get("status", "UNKNOWN"),
            "_change_sort": change_sort,
            "_volume_sort": volume_sort,
        })
    rows.sort(key=lambda row: (row["_change_sort"], row["_volume_sort"]), reverse=True)
    for row in rows:
        row.pop("_change_sort", None)
        row.pop("_volume_sort", None)
    return pd.DataFrame(rows, columns=[
        "نماد", "تغییر ۲۴ساعته (%)", "حجم ۲۴ساعته (USD)",
        "جهت بازار", "شتاب حجم (برابر)", "وضعیت داده",
    ])
