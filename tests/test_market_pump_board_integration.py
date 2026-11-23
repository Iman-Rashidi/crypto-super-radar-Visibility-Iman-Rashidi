from pathlib import Path
import unittest


class MarketPumpBoardIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = Path("crypto_super_radar/web_app.py").read_text(encoding="utf-8")

    def test_spot_universe_includes_requested_assets(self):
        for symbol in ("PEPEUSDT", "SHIBUSDT", "DOGEUSDT", "BONKUSDT", "WIFUSDT",
                       "FLOKIUSDT", "SUIUSDT", "SOLUSDT", "RENDERUSDT", "FETUSDT", "XRPUSDT"):
            with self.subTest(symbol=symbol):
                self.assertIn(symbol, self.source)

    def test_board_uses_exchange_24h_changes_and_volume_spike(self):
        for marker in ("priceChangePercent", "price24hPcnt", "price_change_24h_pct",
                       "volume_spike_ratio", "_market_volume_spike",
                       "🚀 در حال پامپ / صعود قوی", "🟢 متمایل به صعود",
                       "🔴 فشار فروش / نزولی", "⚪ در حال تثبیت"):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.source)

    def test_board_remains_research_only(self):
        self.assertIn('"trade_authorization": "NO_TRADE"', self.source)


if __name__ == "__main__":
    unittest.main()
