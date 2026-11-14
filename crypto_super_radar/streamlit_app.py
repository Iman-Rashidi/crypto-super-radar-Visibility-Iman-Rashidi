"""Optional Streamlit UI for the Persian CRYPTO SUPER RADAR dashboard.

Install the dashboard extra before launching:
    python -m pip install -e ".[dashboard]"
    streamlit run crypto_super_radar/streamlit_app.py

The default Render blueprint continues to use the lightweight HTTP application.
"""
from __future__ import annotations

import altair as alt
import pandas as pd
import streamlit as st

from crypto_super_radar.dashboard_data import build_dashboard_frames
from crypto_super_radar.indicators import simulate_spot_long
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
        categories = []
        failed_ids = {gate.gate_id for gate in failed}
        if 1 in failed_ids:
            categories.append("کیفیت یا عدم تطابق داده‌ها")
        if 6 in failed_ids:
            categories.append("ریسک امنیتی قرارداد")
        if failed_ids - {1, 6}:
            categories.append("شکست معیارهای بازار، مدل یا ریسک")
        explanation = "، ".join(categories) or "شکست فیلتر اجباری"
        st.error(
            f"معامله مسدود شد: {explanation}. "
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

    candle_frame = getattr(_engine().data, "candle_frames", {}).get(result.asset)
    frames = build_dashboard_frames(result, candles=candle_frame)
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
    blockers = result.metrics.get("decision_blockers") or {}
    if result.signal == Signal.NO_TRADE and blockers:
        failed = blockers.get("failed_filters") or []
        unknown = blockers.get("unknown_filters") or []
        if failed:
            st.error("فیلترهای مردود: " + "، ".join(
                f"{item.get('id')}: {item.get('name')}" for item in failed
            ))
        if unknown:
            st.warning("فیلترهای نامشخص: " + "، ".join(
                f"{item.get('id')}: {item.get('name')}" for item in unknown
            ))
        if blockers.get("confirmation_below_threshold"):
            st.info("امتیاز تأیید به حداقل لازم برای ارائهٔ برنامه پژوهشی نرسیده است.")
        if blockers.get("risk_score_missing"):
            st.warning("Risk Score قابل محاسبه نبوده و به‌عنوان داده نامشخص در نظر گرفته شده است.")
        if blockers.get("risk_score_at_or_above_limit"):
            st.error("Risk Score به سقف ریسک مجاز رسیده یا از آن عبور کرده است.")
        if blockers.get("base_engine_returned_no_trade") and not (
            failed or unknown or blockers.get("confirmation_below_threshold")
            or blockers.get("risk_score_missing") or blockers.get("risk_score_at_or_above_limit")
        ):
            st.info("موتور پایه جهت معاملاتی قابل تأییدی تولید نکرده است.")

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
        "نمای بازار و کف/سقف",
        "ماشین‌حساب سود و زیان",
        "۱۲ فیلتر",
        "خطاها و داده‌های نامشخص",
        "معیارهای کوانت",
        "نهنگ‌ها و احساسات",
        "تحلیل بنیادی",
    ])
    with tabs[0]:
        trend_gate = next((gate for gate in result.gates if gate.gate_id == 3), None)
        regime_gate = next((gate for gate in result.gates if gate.gate_id == 2), None)
        asset_regime = (trend_gate.evidence or {}).get("asset_regime") if trend_gate else None
        market_regime = (regime_gate.evidence or {}).get("regime") if regime_gate else None
        regime_labels = {
            "BULLISH": "صعودی · Bullish",
            "BEARISH": "نزولی · Bearish",
            "NEUTRAL": "خنثی · Neutral",
        }
        r1, r2, r3 = st.columns(3)
        r1.metric("رژیم دارایی", regime_labels.get(asset_regime, "نامشخص · UNKNOWN"))
        r2.metric("رژیم بازار معیار", regime_labels.get(market_regime, "نامشخص · UNKNOWN"))
        r3.metric("قیمت آخرین کندل", "نامشخص" if candle_frame is None or candle_frame.empty else f"{float(candle_frame['close'].iloc[-1]):,.8g} USD")
        st.subheader("کف و سقف مشاهده‌شده")
        st.caption("این اعداد بیشینه و کمینهٔ واقعی کندل‌های موجود در هر بازه‌اند؛ حمایت/مقاومت قطعی یا پیش‌بینی قیمت نیستند. هر بازهٔ ناقص یا قدیمی UNKNOWN نمایش داده می‌شود.")
        if frames["market_ranges"].empty:
            st.warning("تاریخچهٔ معتبر OHLC برای محاسبهٔ کف و سقف در دسترس نیست.")
        else:
            st.dataframe(frames["market_ranges"], hide_index=True, use_container_width=True)
        if candle_frame is not None and not candle_frame.empty:
            required = {"open", "high", "low", "close"}
            if required.issubset(candle_frame.columns):
                chart_data = candle_frame.tail(180).reset_index()
                chart_data = chart_data.rename(columns={chart_data.columns[0]: "timestamp"})
                chart_data["timestamp"] = pd.to_datetime(chart_data["timestamp"], utc=True)
                chart_data["direction"] = chart_data.apply(
                    lambda row: "صعودی" if row["close"] >= row["open"] else "نزولی", axis=1
                )
                wick = alt.Chart(chart_data).mark_rule().encode(
                    x=alt.X("timestamp:T", title="زمان"),
                    y=alt.Y("low:Q", title="قیمت (USD)"),
                    y2="high:Q",
                    color=alt.Color("direction:N", scale=alt.Scale(domain=["صعودی", "نزولی"], range=["#16a34a", "#dc2626"]), legend=None),
                    tooltip=["timestamp:T", "open:Q", "high:Q", "low:Q", "close:Q"],
                )
                body = alt.Chart(chart_data).mark_bar(size=7).encode(
                    x="timestamp:T",
                    y="open:Q",
                    y2="close:Q",
                    color=alt.Color("direction:N", scale=alt.Scale(domain=["صعودی", "نزولی"], range=["#16a34a", "#dc2626"]), legend=None),
                )
                st.altair_chart((wick + body).properties(height=360), use_container_width=True)
            else:
                st.info("ستون‌های کامل OHLC برای نمودار کندلی موجود نیستند.")
    with tabs[1]:
        st.subheader("ماشین‌حساب سناریوی Spot")
        st.caption("این ابزار سناریو را محاسبه می‌کند، نه زمان یا احتمال رسیدن به هدف را پیش‌بینی. فقط Long بدون اهرم؛ Short و سفارش واقعی وجود ندارد.")
        a1, a2, a3 = st.columns(3)
        amount = a1.number_input("سرمایه (EUR)", min_value=0.01, value=10.0, step=1.0)
        move = a2.number_input("تغییر فرضی قیمت (%)", min_value=-99.9, max_value=10000.0, value=10.0, step=1.0)
        horizon_days = a3.number_input("افق سناریو (روز)", min_value=1, max_value=3650, value=14, step=1)
        b1, b2, b3, b4 = st.columns(4)
        buy_fee = b1.number_input("کارمزد خرید (%)", min_value=0.0, max_value=20.0, value=0.10, step=0.05)
        sell_fee = b2.number_input("کارمزد فروش (%)", min_value=0.0, max_value=20.0, value=0.10, step=0.05)
        buy_slippage = b3.number_input("لغزش خرید (%)", min_value=0.0, max_value=20.0, value=0.20, step=0.05)
        sell_slippage = b4.number_input("لغزش فروش (%)", min_value=0.0, max_value=20.0, value=0.20, step=0.05)
        try:
            scenario = simulate_spot_long(
                float(amount), float(move), buy_fee_pct=float(buy_fee),
                sell_fee_pct=float(sell_fee), buy_slippage_pct=float(buy_slippage),
                sell_slippage_pct=float(sell_slippage),
            )
            p1, p2, p3, p4 = st.columns(4)
            p1.metric("سود/زیان خالص", f"{scenario['net_pnl_eur']:+.4f} €")
            p2.metric("ارزش نهایی", f"{scenario['net_proceeds_eur']:.4f} €")
            p3.metric("بازده خالص", f"{scenario['net_return_pct']:+.3f}%")
            p4.metric("رشد لازم برای سربه‌سر", f"{scenario['break_even_price_change_pct']:.3f}%")
            st.write(f"سناریو: سرمایه‌گذاری {amount:.2f} یورو، تغییر فرضی قیمت {move:+.2f}% طی افق انتخابی {int(horizon_days)} روز.")
            st.dataframe(pd.DataFrame([
                {"جزء محاسبه": "کارمزد خرید", "مقدار (EUR)": scenario["buy_fee_eur"]},
                {"جزء محاسبه": "لغزش خرید", "مقدار (EUR)": scenario["buy_slippage_eur"]},
                {"جزء محاسبه": "لغزش فروش", "مقدار (EUR)": scenario["sell_slippage_eur"]},
                {"جزء محاسبه": "کارمزد فروش", "مقدار (EUR)": scenario["sell_fee_eur"]},
            ]), hide_index=True, use_container_width=True)
            st.latex(r"V_{net}=I(1-f_b)(1-s_b)(1+r)(1-s_s)(1-f_s)")
            st.caption("سود خالص = ارزش دریافتی پس از هزینه‌ها − سرمایه اولیه. سربه‌سر از حل V_net = I به‌دست می‌آید. نرخ‌های کارمزد و لغزش صرفاً فرض ورودی‌اند و باید با بازار/صرافی واقعی جایگزین شوند.")
            scenario_rows = []
            for move_pct in (-20.0, -10.0, 0.0, 5.0, 10.0, 20.0):
                row = simulate_spot_long(
                    float(amount), move_pct, buy_fee_pct=float(buy_fee),
                    sell_fee_pct=float(sell_fee), buy_slippage_pct=float(buy_slippage),
                    sell_slippage_pct=float(sell_slippage),
                )
                scenario_rows.append({
                    "تغییر قیمت فرضی": f"{move_pct:+.1f}%",
                    "سود/زیان خالص (EUR)": round(row["net_pnl_eur"], 4),
                    "بازده خالص": f"{row['net_return_pct']:+.3f}%",
                    "افق سناریو (روز)": int(horizon_days),
                })
            st.subheader("جدول سناریوهای مقایسه‌ای")
            st.dataframe(pd.DataFrame(scenario_rows), hide_index=True, use_container_width=True)
        except ValueError as exc:
            st.error(f"ورودی ماشین‌حساب معتبر نیست: {exc}")
        st.warning("مالیات، تغییر نرخ EUR/USD، هزینه برداشت/شبکه، اسپردِ فراتر از لغزش ورودی و اثر اندازه سفارش لحاظ نشده‌اند. سود یا زمان سود تضمین نمی‌شود.")
    with tabs[2]:
        st.dataframe(frames["gates"], hide_index=True, use_container_width=True)
    with tabs[3]:
        if frames["errors"].empty:
            st.success("در این اسکن فیلتر FAIL یا UNKNOWN وجود ندارد.")
        else:
            st.dataframe(frames["errors"], hide_index=True, use_container_width=True)
    with tabs[4]:
        st.dataframe(frames["quantitative"], hide_index=True, use_container_width=True)
        st.caption("RSI، EMA، ATR و معیارهای بک‌تست مستقیماً از شواهد همان اسکن استخراج شده‌اند.")
    with tabs[5]:
        st.dataframe(frames["whale_sentiment"], hide_index=True, use_container_width=True)
        st.caption(
            "ورود توکن به صرافی به‌تنهایی اثبات فروش و خروج از صرافی اثبات خرید نیست. "
            "Fear & Greed نیز شاخص کلی بازار است، نه مختص این دارایی."
        )

    with tabs[6]:
        fundamental = result.metrics.get("fundamental_analysis") or {}
        st.subheader("Fundamental Engine | تحلیل بنیادی")
        fc1, fc2 = st.columns(2)
        overall_score = fundamental.get("score")
        coverage = fundamental.get("coverage_pct")
        fc1.metric("امتیاز بنیادی", "نامشخص" if overall_score is None else f"{overall_score:.1f} / 100")
        fc2.metric("پوشش شواهد", "نامشخص" if coverage is None else f"{coverage:.1f}%")
        st.caption("این امتیاز مستقل از Opportunity Rank و Risk Score است و به‌تنهایی مجوز معامله نیست.")
        module_labels = {
            "macro": "اقتصاد کلان",
            "tokenomics": "توکنومیکس",
            "onchain": "داده‌های آن‌چین",
            "project": "سلامت بنیادی پروژه",
            "industry": "تحلیل صنعت و رقبا",
            "dcf": "ارزش‌گذاری DCF",
            "comparables": "مقایسه پروژه‌های هم‌صنعت",
            "accounting_principles": "اصول حسابداری و کیفیت گزارشگری",
            "market_analysis": "تحلیل شرایط و ریسک بازار",
            "event_analysis": "تحلیل رویدادها و بازده غیرعادی",
            "dividend_analysis": "تحلیل سود تقسیمی سهام",
            "share_buybacks": "تحلیل بازخرید سهام",
            "eps_analysis": "تحلیل سود هر سهم (EPS)",
            "mergers_acquisitions": "تحلیل ادغام و تملک شرکت‌ها",
            "sector_rotation": "تحلیل چرخش سرمایه بین بخش‌ها",
        }
        module_rows = []
        for key, label in module_labels.items():
            item = (fundamental.get("modules") or {}).get(key) or {}
            value = item.get("score")
            module_rows.append({
                "ماژول": label,
                "وضعیت": item.get("status", "UNKNOWN"),
                "امتیاز": "نامشخص" if value is None else round(float(value), 2),
            })
        st.dataframe(pd.DataFrame(module_rows), hide_index=True, use_container_width=True)
        accounting = (fundamental.get("modules") or {}).get("accounting_principles") or {}
        market = (fundamental.get("modules") or {}).get("market_analysis") or {}
        with st.expander("اصول حسابداری | شناسایی درآمد، موجودی و استهلاک", expanded=False):
            for key, item in (accounting.get("principles") or {}).items():
                st.markdown(f"**{item.get('label_fa', key)}** · {item.get('status', 'UNKNOWN')}")
                checks = item.get("checks") or {}
                if checks:
                    st.dataframe(pd.DataFrame(
                        [{"کنترل": name, "وضعیت": status} for name, status in checks.items()]
                    ), hide_index=True, use_container_width=True)
                if item.get("reported_method"):
                    st.caption("روش گزارش‌شده: " + item["reported_method"])
                if item.get("note"):
                    st.caption(item["note"])
                if item.get("reason"):
                    st.caption(item["reason"])
        with st.expander("تحلیل بازار | اقتصاد، روندها، مقررات و ریسک", expanded=False):
            observations = market.get("observations") or {}
            st.dataframe(pd.DataFrame([
                {"عامل": item.get("label_fa", key),
                 "وضعیت": item.get("status", "UNKNOWN"),
                 "مقدار": item.get("value"),
                 "منبع": item.get("source"),
                 "زمان مشاهده": item.get("observed_at_utc")}
                for key, item in observations.items()
            ]), hide_index=True, use_container_width=True)
            st.caption("مراحل: گردآوری داده، شناسایی روند، ارزیابی ریسک و ساخت سناریو. پیش‌بینی بدون منبع و زمان مشاهده نمایش داده نمی‌شود.")
        dividend = (fundamental.get("modules") or {}).get("dividend_analysis") or {}
        with st.expander("تحلیل سود تقسیمی | Dividend Analysis", expanded=False):
            st.write(dividend.get("message_fa", "برای تحلیل سود تقسیمی، داده معتبر شرکت لازم است."))
            if dividend.get("status") == "NOT_APPLICABLE":
                st.info("این دارایی رمزارز/توکن است؛ سود تقسیمی شرکتی برای آن کاربرد ندارد.")
            else:
                dc1, dc2, dc3 = st.columns(3)
                dc1.metric("بازده تقسیمی", "نامشخص" if dividend.get("dividend_yield_pct") is None else f"{dividend['dividend_yield_pct']:.2f}%")
                dc2.metric("نسبت پرداخت", "نامشخص" if dividend.get("payout_ratio_pct") is None else f"{dividend['payout_ratio_pct']:.2f}%")
                dc3.metric("پوشش سود", "نامشخص" if dividend.get("dividend_coverage_ratio") is None else f"{dividend['dividend_coverage_ratio']:.2f}x")
                st.write("پایداری پرداخت:", dividend.get("dividend_sustainability", "UNKNOWN"))
                st.write("رشد سالانه مرکب:", dividend.get("dividend_growth_cagr_pct"))
                st.warning("احتمال تله سود تقسیمی: بازده بالا هم‌زمان با افت شدید قیمت شناسایی شد.") if dividend.get("dividend_trap_warning") else None
                st.dataframe(pd.DataFrame([{"کنترل": key, "وضعیت": value} for key, value in (dividend.get("checks") or {}).items()]), hide_index=True, use_container_width=True)
                if dividend.get("source"): st.caption("منبع: " + dividend["source"])


        buyback = (fundamental.get("modules") or {}).get("share_buybacks") or {}
        with st.expander("تحلیل بازخرید سهام | Share Buyback Analysis", expanded=False):
            st.write(buyback.get("message_fa", "برای تحلیل بازخرید، گزارش خریدهای اجراشده و اطلاعات تعداد سهام لازم است."))
            if buyback.get("status") == "NOT_APPLICABLE":
                st.info("بازخرید سهام شرکتی برای توکن کاربرد ندارد.")
            else:
                bc1, bc2, bc3 = st.columns(3)
                bc1.metric("بازده بازخرید", "نامشخص" if buyback.get("buyback_yield_pct") is None else f"{buyback['buyback_yield_pct']:.2f}%")
                bc2.metric("کاهش تعداد سهام", "نامشخص" if buyback.get("share_reduction_pct") is None else f"{buyback['share_reduction_pct']:.2f}%")
                bc3.metric("تأمین مالی بدهی", "نامشخص" if buyback.get("debt_funding_pct") is None else f"{buyback['debt_funding_pct']:.2f}%")
                st.write("صرف/تخفیف قیمت بازخرید نسبت به قیمت مرجع (%):", buyback.get("repurchase_premium_pct"))
                st.write("افزایش مکانیکی EPS با فرض سود ثابت (%):", buyback.get("mechanical_eps_accretion_pct"))
                if buyback.get("review_flags"):
                    st.warning("پرچم‌های بررسی: " + ", ".join(buyback["review_flags"]))
                st.dataframe(pd.DataFrame([{"کنترل": key, "وضعیت": value} for key, value in (buyback.get("checks") or {}).items()]), hide_index=True, use_container_width=True)
                st.caption("صرفاً بازخرید انجام‌شده محاسبه می‌شود؛ مجوز برنامه بازخرید به‌معنای اجرای آن نیست.")
                if buyback.get("source"): st.caption("منبع: " + buyback["source"])
        eps = (fundamental.get("modules") or {}).get("eps_analysis") or {}
        with st.expander("تحلیل سود هر سهم | EPS Analysis", expanded=False):
            st.write(eps.get("message_fa", "برای محاسبه EPS به سود خالص و میانگین تعداد سهام نیاز است."))
            if eps.get("status") == "NOT_APPLICABLE":
                st.info("EPS شرکتی برای توکن کاربرد ندارد.")
            else:
                ec1, ec2, ec3 = st.columns(3)
                ec1.metric("EPS پایه", "نامشخص" if eps.get("basic_eps") is None else f"{eps['basic_eps']:.4f}")
                ec2.metric("EPS رقیق‌شده", "نامشخص" if eps.get("diluted_eps") is None else f"{eps['diluted_eps']:.4f}")
                ec3.metric("P/E", "نامشخص" if eps.get("pe_ratio") is None else f"{eps['pe_ratio']:.2f}x")
                st.write("EPS تعدیل‌شده:", eps.get("adjusted_eps"))
                st.write("KGV / P/E:", "نامشخص" if eps.get("pe_ratio") is None else f"{eps['pe_ratio']:.2f}x")
                st.write("نرخ رشد مورد انتظار EPS (%):", eps.get("expected_eps_growth_pct"))
                st.write("PEG:", "نامشخص" if eps.get("peg_ratio") is None else f"{eps['peg_ratio']:.2f}")
                st.write("تفسیر آموزشی PEG:", eps.get("peg_interpretation", "UNKNOWN"))
                st.write("مقایسه KGV با صنعت / تاریخچه / بازار (%):", eps.get("relative_pe_pct") or "نامشخص")
                st.write("CAGR تاریخی EPS (%):", eps.get("eps_history_cagr_pct"))
                st.write("تغییر EPS پیش‌بینی‌شده نسبت به واقعی (%):", eps.get("forecast_change_pct"))
                st.dataframe(pd.DataFrame([{"کنترل": key, "وضعیت": value} for key, value in (eps.get("checks") or {}).items()]), hide_index=True, use_container_width=True)
                for limitation in eps.get("limitations") or []:
                    st.caption("• " + limitation)
                if eps.get("source"): st.caption("منبع: " + eps["source"])
        esg = (fundamental.get("modules") or {}).get("esg_analysis") or {}
        with st.expander("تحلیل محیط‌زیست، مسئولیت اجتماعی و حاکمیت | ESG", expanded=False):
            st.write(esg.get("message_fa", "برای ارزیابی ESG به داده‌های منبع‌دار نیاز است."))
            if esg.get("status") == "UNKNOWN":
                st.info("اطلاعات معتبر ESG کافی نیست؛ امتیاز ساختگی نمایش داده نمی‌شود.")
            else:
                ec1, ec2, ec3 = st.columns(3)
                for col, key, title in zip((ec1, ec2, ec3), ("environmental", "social", "governance"),
                                           ("محیط‌زیست", "اجتماعی", "حاکمیت")):
                    pillar = (esg.get("pillars") or {}).get(key) or {}
                    val = pillar.get("score")
                    col.metric(title, "نامشخص" if val is None else f"{val:.1f}/100")
                st.metric("میانگین تشخیصی ESG", "نامشخص" if esg.get("diagnostic_score") is None else f"{esg['diagnostic_score']:.1f}/100")
                if esg.get("network_metrics"):
                    st.write("شاخص‌های اختصاصی شبکه رمزارز")
                    st.json(esg["network_metrics"])
                if esg.get("controversies"):
                    st.warning("موارد مناقشه برای بررسی انسانی: " + "؛ ".join(esg["controversies"]))
                for limitation in esg.get("limitations") or []:
                    st.caption("• " + limitation)
                if esg.get("source"):
                    st.caption("منبع: " + esg["source"])
        ma = (fundamental.get("modules") or {}).get("mergers_acquisitions") or {}
        with st.expander("تحلیل ادغام و تملک | M&A Analysis", expanded=False):
            st.write(ma.get("message_fa", "برای تحلیل M&A به شواهد معامله نیاز است."))
            if ma.get("status") == "NOT_APPLICABLE":
                st.info("این ماژول مربوط به ادغام و تملک شرکت‌هاست و برای توکن به‌صورت خودکار امتیاز منفی ایجاد نمی‌کند.")
            else:
                ma_rows = []
                for deal_id, deal in (ma.get("deals") or {}).items():
                    ma_rows.append({"معامله": deal_id, "هدف": deal.get("target"),
                        "خریدار": deal.get("acquirer"), "وضعیت": deal.get("deal_status"),
                        "صرف پیشنهاد (%)": deal.get("offer_premium_pct"),
                        "تأمین مالی": deal.get("financing_type"),
                        "ریسک ادغام": deal.get("integration_risk"),
                        "وضعیت نهاد ناظر": deal.get("regulatory_status"),
                        "پرچم‌های بررسی": ", ".join(deal.get("review_flags") or []),
                        "منبع": deal.get("source_url")})
                if ma_rows:
                    st.dataframe(pd.DataFrame(ma_rows), hide_index=True, use_container_width=True)
                else:
                    st.info("خبر معامله با منبع و زمان معتبر هنوز ثبت نشده است؛ وضعیت UNKNOWN.")
                for limitation in ma.get("limitations") or []:
                    st.caption("• " + limitation)
        rotation = (fundamental.get("modules") or {}).get("sector_rotation") or {}
        with st.expander("تحلیل چرخش بخشی | Sector Rotation", expanded=False):
            st.write(rotation.get("message_fa", "برای تحلیل چرخش بخشی، بازده و جریان سرمایه با منبع معتبر لازم است."))
            st.write("مرحله چرخه اقتصادی:", rotation.get("economic_phase", "UNKNOWN"))
            sector_rows = [{"بخش": row.get("sector"), "بازده (%)": row.get("return_pct"),
                "بازده معیار (%)": row.get("benchmark_return_pct"),
                "قدرت نسبی (%)": row.get("relative_strength_pct"),
                "تغییر جریان سرمایه (%)": row.get("flow_change_pct"),
                "منبع": row.get("source"), "زمان": row.get("observed_at_utc")}
                for row in rotation.get("sectors") or []]
            if sector_rows:
                st.dataframe(pd.DataFrame(sector_rows), hide_index=True, use_container_width=True)
                st.write("پیشتازها:", ", ".join(rotation.get("leaders") or []))
                st.write("ضعیف‌ترها:", ", ".join(rotation.get("laggards") or []))
            else:
                st.info("داده زمان‌دار و منبع‌دار برای بخش‌ها موجود نیست؛ وضعیت UNKNOWN.")
            for limitation in rotation.get("limitations") or []:
                st.caption("• " + limitation)
        event_module = (fundamental.get("modules") or {}).get("event_analysis") or {}
        with st.expander("تحلیل رویدادها | Event Study و بازده غیرعادی", expanded=False):
            st.write(event_module.get("message_fa", "برای تحلیل رویداد، داده زمان‌دار و بازده تاریخی لازم است."))
            event_rows = []
            for event_id, item in (event_module.get("events") or {}).items():
                event_rows.append({"رویداد": event_id, "نوع": item.get("event_type", "نامشخص"),
                    "وضعیت": item.get("status", "UNKNOWN"), "بازده غیرعادی تجمعی": item.get("cumulative_abnormal_return"),
                    "آماره t": item.get("t_statistic"), "p-value": item.get("p_value_two_sided"),
                    "معنادار در ۵٪": item.get("statistically_significant_5pct"), "منبع": item.get("source_url")})
            if event_rows:
                st.dataframe(pd.DataFrame(event_rows), hide_index=True, use_container_width=True)
            else:
                st.info("رویداد زمان‌دار و داده کافی برای پنجره برآورد/رویداد هنوز موجود نیست؛ وضعیت UNKNOWN است.")
            st.caption("بازده غیرعادی = بازده واقعی − بازده عادی مورد انتظار از مدل بازار. آزمون t دوطرفه برای CAR؛ معناداری آماری اثبات رابطه علّی یا پیش‌بینی سود نیست.")
        if fundamental.get("status") in {"UNKNOWN", "INSUFFICIENT_COVERAGE"}:
            st.warning("داده بنیادی معتبر و کافی برای امتیاز کلی در دسترس نیست؛ این وضعیت به‌عنوان UNKNOWN ثبت می‌شود.")
        if fundamental.get("status") == "PARTIAL":
            st.info("امتیاز بر اساس ماژول‌های معتبر محاسبه شده و بخشی از شواهد هنوز نامشخص است.")
        st.info(fundamental.get("message_fa", "این گزارش صرفاً تحلیلی است و معامله را تأیید نمی‌کند."))
        st.caption("DCF فقط با جریان نقدی منتسب به توکن و نسبت صریح Token Value Capture معتبر است؛ مقایسه ارزش‌گذاری فقط با پروژه‌های هم‌صنعت انجام می‌شود. منابع بنیادی زنده هنوز به این محیط متصل نشده‌اند.")

    st.caption(
        "دادهٔ این صفحه مربوط به آخرین اسکن ذخیره‌شده در همین نشست است. "
        "تاریخچهٔ بلندمدت یا پایش ۲۴/۷ از این رابط تضمین نمی‌شود."
    )


if __name__ == "__main__":
    main()
