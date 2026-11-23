from pathlib import Path
import unittest


class LiveChartFrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (Path(__file__).resolve().parents[1] / "crypto_super_radar" / "web_app.py").read_text(encoding="utf-8")

    def test_live_chart_normalizes_provider_timestamp_and_ohlc_types(self):
        self.assertIn("function normalizeChartPayload(input)", self.source)
        self.assertIn("stamp=Date.parse(stamp)", self.source)
        self.assertIn("stamp*=1000", self.source)
        self.assertIn("open:Number(bar.open)", self.source)
        self.assertIn("close:Number(bar.close)", self.source)
        self.assertIn("candles:unique", self.source)

    def test_chart_errors_are_visible_and_global_async_errors_are_captured(self):
        self.assertIn('id="live-radar-chart-error"', self.source)
        self.assertIn("window.addEventListener('error'", self.source)
        self.assertIn("window.addEventListener('unhandledrejection'", self.source)
        self.assertIn("showChartError(error,'main chart')", self.source)
        self.assertIn("drawUnsafe(normalized)", self.source)


    def test_five_mini_charts_define_wave_and_price_fields_before_render(self):
        start = self.source.index("function renderWaveWatchCard(symbolValue,data)")
        end = self.source.index("async function refreshWaveWatchlist()", start)
        card_renderer = self.source[start:end]
        self.assertIn("const phase=String(", card_renderer)
        self.assertIn("waves=data.elliott_waves||{}", card_renderer)
        self.assertIn("const price=document.createElement('div')", card_renderer)
        self.assertIn("drawMiniChart(mini,data,symbolValue)", card_renderer)
        self.assertIn("Number(bar.time)", card_renderer)
        self.assertNotIn("Date.parse(bar.time)", card_renderer)

    def test_five_symbol_watchlist_normalizes_each_api_payload(self):
        start = self.source.index("async function refreshWaveWatchlist()")
        end = self.source.index("button.addEventListener('click'", start)
        watchlist = self.source[start:end]
        self.assertIn("const data=normalizeChartPayload(raw)", watchlist)

    def test_all_renderers_receive_normalized_live_payload(self):
        self.assertIn("data=normalizeChartPayload(data);latestChartData=data;draw(data);", self.source)
        self.assertIn("drawFutureForecast(data,forecastHorizon)", self.source)
        self.assertIn("async function refreshWaveWatchlist()", self.source)


if __name__ == "__main__":
    unittest.main()
