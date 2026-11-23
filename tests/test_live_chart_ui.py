"""Regression tests for the Persian live chart UI (no network or exchange access)."""
from pathlib import Path
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "crypto_super_radar" / "web_app.py"


class LiveChartUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SOURCE.read_text(encoding="utf-8")

    def test_main_chart_is_mobile_readable(self):
        self.assertIn("#live-candle-chart{display:block;width:100%;min-width:820px;height:480px}", self.source)
        self.assertIn('width="1440" height="560"', self.source)
        self.assertIn("@media(max-width:600px)", self.source)

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

    def test_future_projection_chart_has_four_selectable_horizons_and_two_scenarios(self):
        for horizon in ("15m", "1h", "4h", "1d"):
            self.assertIn('data-forecast-horizon="'+horizon+'"', self.source)
        self.assertIn('id="future-forecast-chart"', self.source)
        self.assertIn("function drawFutureForecast(data,horizon)", self.source)
        self.assertIn("scenario(bull,'#32d99a',[7,6])", self.source)
        self.assertIn("scenario(bear,'#ff647b',[7,6])", self.source)
        self.assertIn("۲۰ کندل واقعی اخیر", self.source)
        self.assertIn("نه پیش‌بینی قطعی", self.source)

    def test_chart_labels_use_collision_spacing_and_hide_minor_pivot_text(self):
        self.assertIn("minGap=26", self.source)
        self.assertIn("point.x-lastWaveLabelX>=95", self.source)
        self.assertIn("rightLabels.forEach", self.source)
        self.assertNotIn("wavePoint?wavePoint.label:(p.kind==='LOW'?'کف':'سقف')", self.source)

    def test_manual_demo_tp_sl_are_populated_even_without_signal_levels(self):
        self.assertIn("const suggestedSl=manualDirection==='LONG'?manualEntryPrice-risk:manualEntryPrice+risk", self.source)
        self.assertIn("const suggestedTp=manualDirection==='LONG'?manualEntryPrice+risk*1.5:manualEntryPrice-risk*1.5", self.source)
        self.assertIn("manualTrade.sl.value=String(sl>0?sl:suggestedSl)", self.source)
        self.assertIn("manualTrade.tp.value=String(tp>0?tp:suggestedTp)", self.source)


if __name__ == "__main__":
    unittest.main()
