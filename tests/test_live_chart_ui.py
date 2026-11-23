"""Regression tests for the Persian live chart UI (no network or exchange access)."""
from pathlib import Path
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "crypto_super_radar" / "web_app.py"


class LiveChartUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SOURCE.read_text(encoding="utf-8")

    def test_main_chart_is_mobile_readable(self):
        self.assertIn("#live-candle-chart{display:block;width:100%;min-width:0;height:500px;touch-action:pan-y;cursor:grab}", self.source)
        self.assertIn('width="1440" height="560"', self.source)
        self.assertIn("@media(max-width:600px)", self.source)

    def test_five_minute_default_and_bounded_chart_request(self):
        self.assertIn('<option value="5m" selected>۵ دقیقه</option>', self.source)
        self.assertIn("new AbortController()", self.source)
        self.assertIn("setTimeout(()=>controller.abort(),7000)", self.source)
        self.assertIn("signal:controller.signal", self.source)
        self.assertIn('id="live-radar-retry"', self.source)
        self.assertIn("retryButton.hidden=false", self.source)
        self.assertIn("مهلت دریافت ۷ ثانیه‌ای تمام شد", self.source)

    def test_live_chart_uses_180_candles_and_120_candle_analysis_window(self):
        radar_source = (SOURCE.parent / "live_market_radar.py").read_text(encoding="utf-8")
        self.assertIn("MAX_CANDLES = 180", radar_source)
        self.assertIn("ANALYSIS_CANDLES = 120", radar_source)
        self.assertIn("analysis_frame = closed.tail(ANALYSIS_CANDLES)", radar_source)
        self.assertIn("limit=180", self.source)
        self.assertIn('query.get("interval", ["5m"])[0]', self.source)

    def test_custom_symbol_search_fits_mobile_width(self):
        self.assertIn(".live-radar-custom-search{display:flex;flex-wrap:wrap", self.source)
        self.assertIn("max-width:100%;min-width:0;overflow:hidden", self.source)
        self.assertIn(".live-radar-custom-search label,.live-radar-custom-search button{width:100%;max-width:100%;min-width:0", self.source)
        self.assertIn(".live-radar-controls{grid-template-columns:minmax(0,1fr)", self.source)

    def test_simple_persian_buy_and_sell_labels_are_present(self):
        self.assertIn("🟢 زمان خوب برای خرید (شروع موج صعودی / الگوی برگشت خریداران)", self.source)
        self.assertIn("🔴 زمان فروش یا خروج (سقف موج / احتمال افت قیمت)", self.source)
        self.assertIn("state==='CONFIRMED'", self.source)

    def test_volume_histogram_and_plain_language_status_are_present(self):
        self.assertIn("حجم معاملات هر کندل", self.source)
        self.assertIn("حجم بازار خوب است (مناسب خرید)", self.source)
        self.assertIn("حجم بازار ضعیف است (مناسب خرید نیست)", self.source)
        self.assertIn("live-volume-status", self.source)

    def test_five_real_mini_candlestick_wave_charts_are_rendered(self):
        for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "LINKUSDT"):
            self.assertIn(symbol, self.source)
        self.assertIn("function drawMiniChart(canvasElement,data)", self.source)
        self.assertIn("drawMiniChart(mini,data)", self.source)
        self.assertIn("data.pivots||[]", self.source)

    def test_mini_chart_grid_is_two_columns_desktop_and_one_mobile(self):
        self.assertIn(".live-wave-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}", self.source)
        self.assertIn(".live-wave-grid{grid-template-columns:1fr}", self.source)

    def test_future_projection_chart_has_one_scored_scenario_and_exact_price_axis(self):
        for horizon in ("15m", "1h", "4h", "1d", "1w", "1mo", "6mo", "1y"):
            self.assertIn('data-forecast-horizon="'+horizon+'"', self.source)
        self.assertIn('id="future-forecast-chart"', self.source)
        self.assertIn("function drawFutureForecast(data,horizon)", self.source)
        self.assertIn("const totalScore=scoreParts.reduce", self.source)
        self.assertIn("const color=bullish?'#32d99a':bearish?'#ff647b':'#ffc66d'", self.source)
        self.assertIn("forecast.confidence_band", self.source)
        self.assertIn("خط ابطال پیش‌بینی", self.source)
        self.assertIn("نقطه شروع صعود", self.source)
        self.assertIn("شروع رکود / چرخش", self.source)
        self.assertIn("قدرت سناریو", self.source)
        self.assertIn("future-forecast-evidence", self.source)
        self.assertIn("ctx.fillText(fmt(value),w-right+9,yy+4)", self.source)
        self.assertIn("'هدف '+fmt(target)", self.source)
        self.assertIn("مسیر واحد", self.source)
        self.assertNotIn("scenario(bull", self.source)
        self.assertNotIn("scenario(bear", self.source)
        self.assertIn("const horizonConfig={", self.source)
        self.assertIn("ATR کلان", self.source)
        self.assertIn("نه پیش‌بینی قطعی", self.source)

    def test_touch_pan_zoom_and_return_to_live_controls_are_present(self):
        for control in ("live-chart-zoom-in", "live-chart-zoom-out", "live-chart-now", "live-chart-time-slider", "forecast-chart-zoom-in", "forecast-chart-zoom-out"):
            self.assertIn('id="' + control + '"', self.source)
        for event in ("touchstart", "touchmove", "touchend", "mousedown", "mousemove", "wheel"):
            self.assertIn("addEventListener(\'" + event + "\'", self.source)
        self.assertIn("pinch", self.source.lower()) if "pinch" in self.source.lower() else self.assertIn("lastDistance", self.source)
        self.assertIn("height:500px", self.source)
        self.assertIn("height:420px", self.source)

    def test_future_scenario_uses_wave_volume_candles_rsi_and_moving_average_evidence(self):
        for evidence in ("ساختار امواج", "حجم خرید و فروش", "الگوی کندلی", "واگرایی RSI", "شیب میانگین متحرک"):
            self.assertIn(evidence, self.source)
        self.assertIn("const buyVolume=", self.source)
        self.assertIn("const sellVolume=", self.source)
        self.assertIn("const maSlope=", self.source)

    def test_wave_pivots_have_full_persian_labels_and_prices(self):
        for label in ("مبدأ موج", "سقف موج ۱", "کف اصلاح ۲", "سقف موج ۳", "کف اصلاح ۴", "سقف موج ۵"):
            self.assertIn(label, self.source)
        self.assertIn("+' · '+number(p.price)", self.source)

    def test_chart_labels_use_collision_spacing_and_hide_minor_pivot_text(self):
        self.assertIn("minGap=26", self.source)
        self.assertIn("rightLabels.forEach", self.source)
        self.assertIn("const wavePoints=(data.wave_pivots||[]).slice(-6)", self.source)
        self.assertIn("const labelY=Math.max(pad.top+16", self.source)
        self.assertNotIn("wavePoint?wavePoint.label:(p.kind==='LOW'?'کف':'سقف')", self.source)

    def test_manual_demo_tp_sl_are_populated_even_without_signal_levels(self):
        self.assertIn("const suggestedSl=manualDirection==='LONG'?manualEntryPrice-risk:manualEntryPrice+risk", self.source)
        self.assertIn("const suggestedTp=manualDirection==='LONG'?manualEntryPrice+risk*1.5:manualEntryPrice-risk*1.5", self.source)
        self.assertIn("manualTrade.sl.value=String(sl>0?sl:suggestedSl)", self.source)
        self.assertIn("manualTrade.tp.value=String(tp>0?tp:suggestedTp)", self.source)


if __name__ == "__main__":
    unittest.main()
