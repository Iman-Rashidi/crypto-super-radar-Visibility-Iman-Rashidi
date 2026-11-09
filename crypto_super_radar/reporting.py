"""Persian-first Pandas report and detailed gate explanations."""

from __future__ import annotations

from typing import Iterable, List

import pandas as pd

from .models import AnalysisResult


PERSIAN_COLUMNS = {
    "asset": "دارایی / بازار",
    "signal": "تصمیم نهایی",
    "safety_status": "وضعیت ایمنی",
    "confirmation_score": "امتیاز تأیید از ۱۰۰",
    "estimated_risk_score": "ریسک تخمینی از ۱۰۰؛ بیشتر یعنی خطر بیشتر",
    "opportunity_rank": "رتبه فرصت؛ امتیاز تحلیلی، نه احتمال سود",
    "entry_price_usd": "قیمت ورود دلاری",
    "take_profit_usd": "حد سود دلاری",
    "stop_loss_usd": "حد ضرر دلاری",
    "expected_holding_days": "مدت نگهداری مورد انتظار به روز",
    "position_notional_usd": "ارزش اسمی موقعیت؛ سفارش واقعی نیست",
    "risk_budget_usd": "بودجه زیان مجاز در مدل آزمایشی",
    "gate_pass_count": "تعداد فیلترهای تأییدشده",
    "gate_fail_count": "تعداد فیلترهای ردشده",
    "gate_unknown_count": "تعداد فیلترهای نامشخص",
    "market_regime": "رژیم کلی بازار بر پایه BTC",
    "rsi14": "شاخص قدرت نسبی ۱۴ دوره‌ای",
    "relative_strength_30d_pct": "قدرت نسبی ۳۰ روزه در برابر BTC",
    "atr14": "میانگین دامنه واقعی ۱۴ روزه؛ معیار نوسان",
    "volume_to_liquidity": "نسبت حجم ۲۴ ساعته به نقدینگی",
    "price_discrepancy_pct": "اختلاف قیمت دو منبع به درصد",
    "volume_discrepancy_pct": "اختلاف حجم دو منبع به درصد",
    "sentiment_score": "امتیاز احساسات تیترهای ورودی از صفر تا ۱۰۰",
    "hype_score": "شدت توجه/هیجان در تیترهای ورودی؛ جهش تاریخی را اثبات نمی‌کند",
    "leader_mentions": "تعداد اشاره به چهره‌های اثرگذار بازار مانند Elon Musk",
    "oos_sharpe": "شارپ خارج از نمونه پس از هزینه مفروض",
    "oos_sortino": "سورتینو خارج از نمونه پس از هزینه مفروض",
    "oos_profit_factor": "ضریب سود به زیان خارج از نمونه",
    "oos_max_drawdown_pct": "حداکثر افت سرمایه خارج از نمونه به درصد",
    "oos_net_return_pct": "بازده خالص خارج از نمونه به درصد",
    "pool_created_at": "زمان ایجاد استخر؛ در صورت نبود داده نامشخص",
    "provider_a": "منبع داده اول",
    "provider_b": "منبع داده دوم",
    "security_status": "وضعیت بررسی امنیت دارایی",
    "funding_rate_8h": "نرخ Funding هر ۸ ساعت؛ فقط قرارداد دائمی",
    "open_interest_binance": "موقعیت‌های باز در Binance Futures",
    "open_interest_bybit": "موقعیت‌های باز در Bybit Linear",
    "backtest_status": "نتیجه اعتبارسنجی آماری",
}


def results_dataframe(results: Iterable[AnalysisResult]) -> pd.DataFrame:
    # Append to a single master list outside iteration; no loop overwrite bug.
    records: List[dict] = []
    for result in results:
        records.append(result.to_record())
    frame = pd.DataFrame.from_records(records)
    return frame


def print_report(frame: pd.DataFrame) -> None:
    if frame.empty:
        print("هیچ گزارشی برای نمایش وجود ندارد.")
        return
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 240)
    pd.set_option("display.max_colwidth", 120)
    print("\n╔══════════════════════════════════════════════════════════════╗")
    print("║              CRYPTO SUPER RADAR — گزارش کمی                 ║")
    print("╚══════════════════════════════════════════════════════════════╝")
    print("قانون سیستم: هر ۹ فیلتر باید PASS باشند؛ FAIL یا UNKNOWN یعنی NO TRADE.")
    print("LONG = خرید | SHORT = فروش استقراضی در بازار مشتقه | NO TRADE = عدم ورود")
    print("LOW_RISK فقط برچسب نسبی مدل است، نه تضمین ایمنی یا سود.\n")
    visible = [c for c in frame.columns if c != "gates"]
    print(frame[visible].to_string(index=False))
    print("\nتوضیح فیلترها:")
    for _, row in frame.iterrows():
        print(f"\nدارایی: {row.get('asset', 'UNKNOWN')} | تصمیم: {row.get('signal', 'NO TRADE')}")
        for item in row.get("gates", []) or []:
            print(f"  [{item['status']}] مرحله {item['gate_id']}: {item['name_fa']} — {item['reason_fa']}")
        print(f"  توضیح نهایی: {row.get('summary_fa', '')}")
    print("\nواژه‌نامه ستون‌ها:")
    for column in visible:
        if column in PERSIAN_COLUMNS:
            print(f"  • {column}: {PERSIAN_COLUMNS[column]}")
