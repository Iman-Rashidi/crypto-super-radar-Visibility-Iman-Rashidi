"""Optional Streamlit UI for the Persian CRYPTO SUPER RADAR dashboard.

Install the dashboard extra before launching:
    python -m pip install -e ".[dashboard]"
    streamlit run crypto_super_radar/streamlit_app.py

The default Render blueprint continues to use the lightweight HTTP application.
"""
from __future__ import annotations

import streamlit as st

from crypto_super_radar.dashboard_data import build_dashboard_frames
from crypto_super_radar.integrated_engine import WhaleAwareConfirmationEngine
from crypto_super_radar.models import AssetSpec, GateStatus, Signal


st.set_page_config(
    page_title="CRYPTO SUPER RADAR",
    page_icon="🐋",
    layout="wide",
    initial_sidebar_state="expanded",
)


@st.cache_resource(show_spinner=False)
def _engine() -> WhaleAwareConfirmationEngine:
    """Reuse HTTP clients, rate limits and the local SQLite store in this process."""
    return WhaleAwareConfirmationEngine()


def _lines(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


def _show_gate_status(result) -> None:
    failed = [gate for gate in result.gates if gate.status == GateStatus.FAIL]
    unknown = [gate for gate in result.gates if gate.status == GateStatus.UNKNOWN]
    if failed:
        reasons = "، ".join(f"{gate.gate_id}: {gate.name_fa}" for gate in failed)
        st.error(
            "معامله مسدود شد: حداقل یک فیلتر اجباری شکست خورده است. "
            "به دلیل ریسک امنیتی یا عدم تطابق داده‌ها، معامله مسدود شد. "
            f"فیلترهای ناموفق: {reasons}"
        )
    elif unknown:
        reasons = "، ".join(f"{gate.gate_id}: {gate.name_fa}" for gate in unknown)
        st.warning(
            "معامله مسدود شد: شواهد کافی برای تأیید تمام فیلترها وجود ندارد. "
            f"موارد نامشخص: {reasons}"
        )
    elif result.signal == Signal.NO_TRADE:
        st.info(
            "هر ۱۲ فیلتر تأیید شده‌اند، اما آستانهٔ امتیاز فرصت یا محدودیت ریسک "
            "برای ارائهٔ برنامهٔ پژوهشی برآورده نشده است."
        )
    else:
        st.success(
            "هر ۱۲ فیلتر تأیید شده‌اند. این خروجی فقط برنامهٔ پژوهشی/Paper است؛ "
            "هیچ سفارشی ارسال نشده است."
        )


def main() -> None:
    st.title("🐋 CRYPTO SUPER RADAR")
    st.caption(
        "سامانهٔ پژوهش کوانت و هوش بازار رمزارز | افق Swing و Position | "
        "فقط تحلیل و Paper Research؛ بدون اتصال کیف‌پول یا ارسال سفارش"
    )

    with st.sidebar:
        st.header("تنظیم اسکن")
        asset_text = st.text_input(
            "دارایی و بازار",
            value="cex:BTCUSDT",
            help="نمونه: cex:BTCUSDT، perp:BTCUSDT، solana:MINT، base:0xTOKEN",
        )
        headline_text = st.text_area(
            "تیترهای خبری تکمیلی",
            placeholder="هر تیتر را در یک خط وارد کنید",
            height=100,
        )
        social_text = st.text_area(
            "متن‌های اجتماعی تکمیلی",
            placeholder="هر متن را در یک خط وارد کنید؛ این ورودی جای RSS اجتماعی اجباری را نمی‌گیرد",
            height=100,
        )
        portfolio_risk = st.number_input(
            "ریسک باز پرتفوی (درصد)",
            min_value=0.0,
            max_value=100.0,
            value=0.0,
            step=0.25,
        )
        run_scan = st.button("اجرای اسکن پژوهشی", type="primary", use_container_width=True)
        st.caption(
            "داده‌های عمومی ممکن است محدود، ناقص یا موقتاً در دسترس نباشند. "
            "UNKNOWN به معنی تأیید نشدن است، نه صفر."
        )

    if run_scan:
        try:
            asset = AssetSpec.parse(asset_text.strip())
        except ValueError as exc:
            st.error(f"فرمت دارایی معتبر نیست: {exc}")
            return

        try:
            with st.spinner("در حال دریافت داده‌های بازار، نهنگ‌ها و احساسات..."):
                result = _engine().analyze(
                    asset,
                    headlines=_lines(headline_text),
                    social_posts=_lines(social_text),
                    open_portfolio_risk_pct=float(portfolio_risk),
                )
            st.session_state["csr_last_result"] = result
        except Exception as exc:
            st.error(
                "اسکن کامل نشد. دادهٔ ناقص یا خطای منبع نباید به سیگنال تبدیل شود. "
                f"جزئیات فنی: {type(exc).__name__}: {exc}"
            )
            return

    result = st.session_state.get("csr_last_result")
    if result is None:
        st.info("برای شروع، دارایی را انتخاب کنید و دکمهٔ اجرای اسکن پژوهشی را بزنید.")
        st.markdown(
            """
            **سه خروجی مستقل**

            - Risk Score: برآورد ریسک؛ عدد بالاتر یعنی ریسک بیشتر.
            - Opportunity Rank: رتبهٔ فرصت، جدا از Risk Score.
            - Confidence Level: پوشش شواهد پس از جریمهٔ واگرایی؛ احتمال سود نیست.

            **قانون توقف:** وجود FAIL یا UNKNOWN در هر یک از ۱۲ فیلتر، خروجی را به NO TRADE تبدیل می‌کند.
            """
        )
        return

    frames = build_dashboard_frames(result)
    st.subheader(f"نتیجهٔ پژوهش: {result.asset}")
    _show_gate_status(result)

    risk = result.metrics.get("risk_score", result.estimated_risk_score)
    opportunity = result.opportunity_rank
    confidence = result.metrics.get("confidence_level_pct")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Risk Score", "نامشخص" if risk is None else f"{risk:.1f} / 100")
    c2.metric("Opportunity Rank", "نامشخص" if opportunity is None else f"{opportunity:.1f}")
    c3.metric("Confidence Level", "نامشخص" if confidence is None else f"{confidence:.1f}%")
    c4.metric("تصمیم پژوهشی", result.signal.value)

    st.write(result.summary_fa)
    if result.signal != Signal.NO_TRADE:
        plan = {
            "ورود": result.entry_price,
            "حد ضرر": result.stop_loss,
            "هدف سود": result.take_profit,
            "افق نگهداری (روز)": result.expected_holding_days,
            "اندازه موقعیت شبیه‌سازی‌شده (USD)": result.position_notional_usd,
        }
        st.dataframe(
            [{"پارامتر": key, "مقدار": value} for key, value in plan.items()],
            hide_index=True,
            use_container_width=True,
        )

    tabs = st.tabs([
        "۱۲ فیلتر",
        "خطاها و داده‌های نامشخص",
        "معیارهای کوانت",
        "نهنگ‌ها و احساسات",
    ])
    with tabs[0]:
        st.dataframe(frames["gates"], hide_index=True, use_container_width=True)
    with tabs[1]:
        if frames["errors"].empty:
            st.success("در این اسکن فیلتر FAIL یا UNKNOWN وجود ندارد.")
        else:
            st.dataframe(frames["errors"], hide_index=True, use_container_width=True)
    with tabs[2]:
        st.dataframe(frames["quantitative"], hide_index=True, use_container_width=True)
        st.caption("RSI، EMA، ATR و معیارهای بک‌تست مستقیماً از شواهد همان اسکن استخراج شده‌اند.")
    with tabs[3]:
        st.dataframe(frames["whale_sentiment"], hide_index=True, use_container_width=True)
        st.caption(
            "ورود توکن به صرافی به‌تنهایی اثبات فروش و خروج از صرافی اثبات خرید نیست. "
            "Fear & Greed نیز شاخص کلی بازار است، نه مختص این دارایی."
        )

    st.caption(
        "دادهٔ این صفحه مربوط به آخرین اسکن ذخیره‌شده در همین نشست است. "
        "تاریخچهٔ بلندمدت یا پایش ۲۴/۷ از این رابط تضمین نمی‌شود."
    )


if __name__ == "__main__":
    main()
