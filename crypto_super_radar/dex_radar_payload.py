"""Build a truthful three-panel radar payload from DEX Screener spot data."""
from __future__ import annotations

from typing import Any


def build_dex_radar_payload(asset: Any, report: dict[str, Any]) -> dict[str, Any]:
    report = report if isinstance(report, dict) else {}
    price = report.get("price_usd")
    changes = report.get("price_change_pct") or {}
    periods = {"5m": "m5", "1h": "h1", "6h": "h6", "24h": "h24"}
    timeframe_ranges = {
        label: {
            "status": "NOT_AVAILABLE",
            "low": "NOT_AVAILABLE",
            "high": "NOT_AVAILABLE",
            "change_pct": changes.get(source, "NOT_AVAILABLE"),
            "bar_count": 0,
            "expected_bars": 0,
            "coverage_pct": 0,
            "reason": "DexScreener تغییر درصدی را ارائه می‌کند، نه OHLC یا کف/سقف واقعی این بازه.",
        }
        for label, source in periods.items()
    }
    missing = [key for key, source in periods.items() if changes.get(source) is None]
    missing.extend(key for key, value in (
        ("price", price), ("liquidity", report.get("liquidity_usd")),
        ("volume_24h", report.get("volume_24h_usd")),
    ) if value is None)
    side_sources = {
        "security": report.get("red_flag_status", "UNKNOWN"),
        "whale": (report.get("whale_flow") or {}).get("status", "NOT_AVAILABLE"),
        "technical_ohlc": "NOT_AVAILABLE",
        "independent_market_validation": "NOT_AVAILABLE",
    }
    unavailable = [name for name, status in side_sources.items()
                   if str(status).upper() not in {"PASS", "KNOWN", "COMPLETE", "AVAILABLE"}]
    status = "PARTIAL" if missing or unavailable else "COMPLETED"
    symbol = report.get("symbol") or getattr(asset, "symbol", None) or "UNKNOWN"
    name = report.get("token_name") or "نامشخص"
    diagnosis = {
        "coin_name": name,
        "symbol": symbol,
        "final_recommendation": "تحلیل جزئی؛ تا تکمیل داده‌ها معامله نکن",
        "overall_signal_fa": "بدون معامله · NO_TRADE",
        "explanation": "دادهٔ قیمت و تغییرات بازار از یک استخر DEX دریافت شده؛ این داده به‌تنهایی تأیید مستقل یا مجوز معامله نیست.",
        "short_term": {"action_fa": "فعلاً ورود انجام نشود؛ دادهٔ OHLC و تأیید مستقل موجود نیست.", "reasons": ["سیگنال فقط بر پایهٔ دادهٔ یک استخر است."]},
        "long_term": {"action_fa": "برای ارزیابی نگهداری بلندمدت، دادهٔ تاریخی و بنیادی کافی نیست.", "reasons": ["تاریخچهٔ معتبر و ممیزی مستقل در دسترس نیست."]},
        "blockers": [{"name": key, "status": "NOT_AVAILABLE", "reason": "دادهٔ معتبر این بخش در مسیر اسکن سریع موجود نیست."} for key in unavailable + missing],
        "candle_age_text": "سن کندل: NOT_AVAILABLE؛ دادهٔ کندلی OHLC ارائه نشده است.",
    }
    market_overview = {
        "status": status,
        "reason": "منابع ناموجود: " + ", ".join(unavailable + missing) if unavailable or missing else None,
        "current_price": price if price is not None else "NOT_AVAILABLE",
        "candle_age_hours": "NOT_AVAILABLE",
        "ranges": {key: {"status": "NOT_AVAILABLE", "low": "NOT_AVAILABLE", "high": "NOT_AVAILABLE",
                         "change_pct": changes.get(source, "NOT_AVAILABLE"), "candle_count": 0,
                         "coverage_pct": 0} for key, source in periods.items()},
        "candles": [],
        "asset_regime": "NOT_AVAILABLE",
        "benchmark_regime": "NOT_AVAILABLE",
        "chart_pattern": {"status": "NOT_AVAILABLE", "pattern": "NOT_AVAILABLE"},
    }
    return {
        "status": status,
        "partial_reasons": unavailable + missing,
        "asset": getattr(asset, "raw", report.get("asset", "")),
        "coin_name": name,
        "coin_name_en": name,
        "symbol": symbol,
        "signal": "NO_TRADE",
        "summary_fa": report.get("message_fa") or "گزارش DEX با محدودیت منابع تکمیل شد.",
        "diagnosis": diagnosis,
        "timeframe_ranges": timeframe_ranges,
        "market_overview": market_overview,
        "market_intelligence": {
            "status": "PARTIAL",
            "dex_screener": {"token_name": name, "token_symbol": symbol,
                "price_usd": price, "liquidity_usd": report.get("liquidity_usd"),
                "volume_24h_usd": report.get("volume_24h_usd"),
                "price_change_pct": changes},
            "coinglass": {"status": "NOT_AVAILABLE", "reason": "منبع مشتقات در مسیر اسکن DEX دریافت نشده است."},
            "scoring": {"status": "PARTIAL", "notes": ["امتیاز Gem Hunter جایگزین تأیید مستقل بازار نیست."]},
        },
        "gates": [],
        "confirmation_score": None,
        "estimated_risk_score": report.get("risk_score"),
        "opportunity_rank": None,
        "safety": {"read_only": True, "public_data_only": True, "orders_submitted": False,
                   "transactions_signed": False, "trade_authorization": "NO_TRADE"},
    }
