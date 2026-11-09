"""Pandas adapters for the optional Persian Streamlit dashboard.

These functions are deliberately independent of Streamlit so they can be
unit-tested in the base, zero-cost installation.
"""
from __future__ import annotations

import json
from typing import Any

import pandas as pd

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


def build_dashboard_frames(result: AnalysisResult) -> dict[str, pd.DataFrame]:
    """Build all source-backed dashboard tables from one engine result."""
    return {
        "gates": gates_dataframe(result),
        "errors": data_errors_dataframe(result),
        "quantitative": quantitative_dataframe(result),
        "whale_sentiment": whale_sentiment_dataframe(result),
    }
