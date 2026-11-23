"""Regression tests for the Persian live chart UI (no network or exchange access)."""
from pathlib import Path
from http.server import ThreadingHTTPServer
import re
import shutil
import subprocess
import tempfile
import threading
from urllib.request import urlopen
import unittest

from crypto_super_radar.web_app import Handler


SOURCE = Path(__file__).resolve().parents[1] / "crypto_super_radar" / "web_app.py"


class LiveChartUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SOURCE.read_text(encoding="utf-8")

    def test_rendered_homepage_inline_scripts_pass_node_syntax_check(self):
        node = shutil.which("node")
        self.assertIsNotNone(
            node,
            "Node.js is required for the rendered dashboard JavaScript syntax guard",
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urlopen(f"http://127.0.0.1:{server.server_address[1]}/", timeout=10) as response:
                self.assertEqual(response.status, 200)
                html = response.read().decode("utf-8")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

        scripts = re.findall(r"<script\\b[^>]*>([\\s\\S]*?)</script\\s*>", html, flags=re.IGNORECASE)
        self.assertTrue(scripts, "The rendered homepage did not contain any script blocks")
        inline_scripts = [script for script in scripts if script.strip()]
        self.assertTrue(inline_scripts, "The rendered homepage contained no inline JavaScript")

        with tempfile.TemporaryDirectory(prefix="csr-js-syntax-") as temp_dir:
            for index, script in enumerate(inline_scripts, start=1):
                script_path = Path(temp_dir) / f"homepage-script-{index}.js"
                script_path.write_text(script, encoding="utf-8")
                checked = subprocess.run(
                    [node, "--check", str(script_path)],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
                self.assertEqual(
                    checked.returncode,
                    0,
                    f"SyntaxError in rendered homepage inline <script> #{index}:\\n"
                    f"{checked.stderr or checked.stdout}",
                )

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

    def test_future_projection_chart_has_one_scored_scenario_and_exact_price_axis(self):
        for horizon in ("15m", "1h", "4h", "1d"):
            self.assertIn('data-forecast-horizon="'+horizon+'"', self.source)
        self.assertIn('id="future-forecast-chart"', self.source)
        self.assertIn("function drawFutureForecast(data,horizon)", self.source)
        self.assertIn("const totalScore=scoreParts.reduce", self.source)
        self.assertIn("const color=bullish?'#32d99a':'#ff647b'", self.source)
        self.assertIn("ctx.fillText(fmt(value),w-right+9,yy+4)", self.source)
        self.assertIn("'هدف '+fmt(target)", self.source)
        self.assertIn("مسیر واحد", self.source)
        self.assertNotIn("scenario(bull", self.source)
        self.assertNotIn("scenario(bear", self.source)
        self.assertIn("۲۰ کندل واقعی اخیر", self.source)
        self.assertIn("نه پیش‌بینی قطعی", self.source)

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
