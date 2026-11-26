"""Quota-free, deterministic, read-only Persian CSR dashboard copilot.

This module deliberately has no external LLM/API dependency. It summarizes only
verified fields supplied by the dashboard context provider.
"""
from __future__ import annotations

import json
from typing import Any, Callable

MAX_MESSAGE_CHARS = 8000
MAX_CONTEXT_CHARS = 48000
ENGINE_NAME = "CSR Local Rules Engine v1"


class CopilotError(Exception):
    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code = code
        self.status = status


def _safe_context(context: Any) -> dict[str, Any]:
    if not isinstance(context, dict):
        return {"context_status": "UNAVAILABLE"}
    try:
        encoded = json.dumps(context, ensure_ascii=False, separators=(",", ":"), default=str)
        if len(encoded) > MAX_CONTEXT_CHARS:
            return {"context_status": "CONTEXT_TOO_LARGE", "read_only": True}
        return context
    except (TypeError, ValueError):
        return {"context_status": "SERIALIZATION_FAILED", "read_only": True}


def _value(value: Any, suffix: str = "") -> str:
    if value is None or value == "":
        return "ناموجود"
    if isinstance(value, bool):
        return "بله" if value else "خیر"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)[:500]
    return f"{value}{suffix}"


def _candidate_table(candidates: list[dict[str, Any]]) -> str:
    if not candidates:
        return (
            "در آخرین گزارش قابل‌دسترسی، نامزد Gem Hunter ثبت نشده است. "
            "این به معنی صفر بودن فرصت‌های بازار نیست؛ یعنی دادهٔ نامزد معتبر در گزارش موجود نیست."
        )
    rows = [
        "| نام | نماد | شبکه | قرارداد | قیمت USD | نقدینگی USD | وضعیت امنیتی | امتیاز ریسک |",
        "|---|---|---|---|---:|---:|---|---:|",
    ]
    for item in candidates[:10]:
        if not isinstance(item, dict):
            continue
        name = _value(item.get("name"))
        symbol = _value(item.get("symbol"))
        chain = _value(item.get("chain"))
        contract = _value(item.get("contract_address_short"))
        price = _value(item.get("price_usd"))
        liquidity = _value(item.get("liquidity_usd"))
        security = _value(item.get("security_status"))
        risk = _value(item.get("risk_score"))
        rows.append(f"| {name} | {symbol} | {chain} | {contract} | {price} | {liquidity} | {security} | {risk} |")
    return "\n".join(rows)


def _gem_hunter_answer(context: dict[str, Any]) -> str:
    gem = context.get("gem_hunter") if isinstance(context.get("gem_hunter"), dict) else {}
    candidates = gem.get("top_candidates") if isinstance(gem.get("top_candidates"), list) else []
    lines = [
        "### گزارش Gem Hunter",
        f"- وضعیت آخرین گزارش: **{_value(gem.get('status'))}**",
        f"- زمان تولید: **{_value(gem.get('generated_at_utc'))}**",
        f"- تعداد کشف‌شده: **{_value(gem.get('discovered_candidate_count'))}**",
        f"- نامزدهای Tier 1: **{_value(gem.get('tier1_count'))}**",
        f"- نامزدهای Tier 2: **{_value(gem.get('tier2_count'))}**",
        "",
        _candidate_table(candidates),
        "",
        "### ریسک‌ها و محدودیت‌ها",
        "- نقدینگی پایین می‌تواند باعث لغزش قیمت و دشواری خروج شود.",
        "- وضعیت امنیتی UNKNOWN به معنی تأیید امنیت نیست؛ تا دریافت شواهد، ریسک را بالا در نظر بگیر.",
        "- تمرکز مالکیت، اختیار Mint/Freeze، Honeypot و مالیات فروش باید از دادهٔ امنیتی واقعی بررسی شوند.",
        "- امتیاز فرصت به‌تنهایی مجوز خرید یا تضمین رشد نیست.",
        "- مجوز معامله در این گزارش: **NO_TRADE**؛ این دستیار فقط‌خواندنی است.",
    ]
    for item in candidates[:5]:
        if not isinstance(item, dict):
            continue
        symbol = _value(item.get("symbol"))
        security = _value(item.get("security_status"))
        lines.append(
            f"- **{symbol}**: Mint فعال={_value(item.get('mint_authority_active'))}؛ "
            f"Freeze فعال={_value(item.get('freeze_authority_active'))}؛ "
            f"Honeypot={_value(item.get('honeypot_detected'))}؛ "
            f"مالیات فروش={_value(item.get('sell_tax_pct'), '%')}؛ "
            f"حجم ۲۴ساعته={_value(item.get('volume_24h_usd'))}؛ "
            f"وضعیت امنیتی={security}."
        )
        reasons = item.get("security_reason_fa") or item.get("reason_fa")
        if reasons:
            lines.append(f"  - شواهد/دلیل ثبت‌شده: {_value(reasons)}")
    return "\n".join(lines)


def _challenge_answer(context: dict[str, Any]) -> str:
    challenge = context.get("challenge1_v2") if isinstance(context.get("challenge1_v2"), dict) else {}
    rows = challenge.get("five_assets") if isinstance(challenge.get("five_assets"), list) else []
    if not rows:
        return (
            "برای CHALLENGE_1_V2 در زمینهٔ فعلی چرخهٔ قابل‌گزارشی موجود نیست. "
            f"وضعیت: **{_value(challenge.get('status'))}**. "
            "بدون رکورد چرخه، دربارهٔ سیگنال، ورود، SL یا TP حدس نمی‌زنم."
        )
    table = [
        "| بازار | سیگنال خام | ورود | حد ضرر | حد سود | وضعیت |",
        "|---|---|---:|---:|---:|---|",
    ]
    for row in rows[:5]:
        if not isinstance(row, dict):
            continue
        table.append(
            f"| {_value(row.get('symbol'))} | {_value(row.get('signal_raw'))} | "
            f"{_value(row.get('entry_price'))} | {_value(row.get('stop_loss'))} | "
            f"{_value(row.get('take_profit'))} | {_value(row.get('status') or row.get('decision'))} |"
        )
    return (
        "### وضعیت CHALLENGE_1_V2\n"
        f"- شناسهٔ آخرین چرخه: **{_value(challenge.get('latest_cycle_id'))}**\n"
        f"- زمان اسکن: **{_value(challenge.get('scan_time_utc'))}**\n"
        f"- وضعیت اجرا: **{_value(challenge.get('run_state'))}**\n\n"
        + "\n".join(table)
        + "\n\nمقادیر ناموجود عمداً ناموجود گزارش شده‌اند؛ قیمت جاری جایگزین قیمت ورود یا حدهای ثبت‌شده نمی‌شود. "
        "این گزارش به‌تنهایی مجوز سفارش نیست."
    )


def _summary_answer(context: dict[str, Any]) -> str:
    health = context.get("data_health") if isinstance(context.get("data_health"), dict) else {}
    gem = context.get("gem_hunter") if isinstance(context.get("gem_hunter"), dict) else {}
    challenge = context.get("challenge1_v2") if isinstance(context.get("challenge1_v2"), dict) else {}
    return "\n".join([
        "### خلاصهٔ وضعیت داشبورد",
        f"- زمان ساخت زمینه: **{_value(context.get('generated_at_utc'))}**",
        f"- سلامت دادهٔ بازار: **{_value(health.get('status'))}**",
        f"- آخرین مشاهدهٔ داده: **{_value(health.get('observed_at_utc'))}**",
        f"- بازارهای مشاهده‌شده: **{_value(health.get('observed_symbols'))}**",
        f"- وضعیت CHALLENGE_1_V2: **{_value(challenge.get('status'))}**",
        f"- وضعیت Gem Hunter: **{_value(gem.get('status'))}**",
        f"- تعداد نامزدهای کشف‌شده: **{_value(gem.get('discovered_candidate_count'))}**",
        "",
        "اگر بخشی از داده ناموجود یا قدیمی باشد، نتیجهٔ آن بخش قابل تأیید نیست. این خلاصه هیچ سفارش یا معامله‌ای اجرا نمی‌کند.",
    ])


def _answer(message: str, context: dict[str, Any]) -> str:
    q = message.casefold()
    if any(term in q for term in ("gem", "جم", "نامزد", "توکن جدید", "توکن‌های جدید")):
        return _gem_hunter_answer(context)
    if any(term in q for term in ("challenge", "چلنج", "v2", "ورود", "سیگنال", "چرا ربات", "معامله")):
        return _challenge_answer(context)
    if any(term in q for term in ("خلاصه", "وضعیت", "داشبورد", "سلامت داده", "بازار الان")):
        return _summary_answer(context)
    return (
        "من دستیار داخلی و بدون API سهمیه‌ای CRYPTO SUPER RADAR هستم. "
        "می‌توانم داده‌های موجود داشبورد را دربارهٔ Gem Hunter، ریسک نامزدها، "
        "وضعیت پنج بازار CHALLENGE_1_V2 و سلامت فید بازار توضیح بدهم.\n\n"
        "برای پاسخ دقیق‌تر، پرسش را به یکی از این موضوعات وصل کن. "
        "اگر دادهٔ لازم در داشبورد نباشد، آن را ناموجود اعلام می‌کنم و چیزی جعل نمی‌کنم."
    )


def chat(message: str, context_provider: Callable[[], dict[str, Any]], *,
         client_key: str = "anonymous", http_post=None) -> dict[str, Any]:
    """Answer from the local dashboard snapshot; no network calls or hourly quota."""
    text = str(message or "").strip()
    if not text:
        raise CopilotError("MESSAGE_REQUIRED", 400)
    if len(text) > MAX_MESSAGE_CHARS:
        raise CopilotError("MESSAGE_TOO_LONG", 413)
    try:
        context = _safe_context(context_provider())
    except Exception as exc:
        context = {"context_status": "UNAVAILABLE", "context_error": type(exc).__name__}
    answer = _answer(text, context)
    return {
        "status": "OK",
        "provider": "داخلی",
        "model": ENGINE_NAME,
        "answer": answer[:16000],
        "read_only": True,
        "orders_submitted": False,
        "quota": {"provider_api": False, "hourly_message_limit": None, "max_message_chars": MAX_MESSAGE_CHARS},
        "data_context_status": context.get("context_status", "AVAILABLE"),
    }
