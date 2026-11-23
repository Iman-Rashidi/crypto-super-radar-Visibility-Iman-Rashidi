import unittest

from crypto_super_radar.dashboard_data import market_board_dataframe, market_movement_label


class MarketMovementLabelTests(unittest.TestCase):
    def test_market_state_thresholds(self):
        self.assertEqual(market_movement_label(5.01), "🚀 در حال پامپ / صعود قوی")
        self.assertEqual(market_movement_label(5.0), "🟢 متمایل به صعود")
        self.assertEqual(market_movement_label(1.0), "🟢 متمایل به صعود")
        self.assertEqual(market_movement_label(0.99), "⚪ در حال تثبیت")
        self.assertEqual(market_movement_label(-3.0), "⚪ در حال تثبیت")
        self.assertEqual(market_movement_label(-3.01), "🔴 فشار فروش / نزولی")

    def test_missing_and_non_finite_changes_are_not_zero(self):
        self.assertIn("ناموجود", market_movement_label(None))
        self.assertIn("ناموجود", market_movement_label(float("nan")))
        self.assertIn("ناموجود", market_movement_label(float("inf")))

    def test_board_sorts_by_change_then_volume(self):
        frame = market_board_dataframe([
            {"symbol": "AAAUSDT", "price_change_24h_pct": 2.0, "volume_24h_usd": 900},
            {"symbol": "BBBUSDT", "price_change_24h_pct": 7.0, "volume_24h_usd": 100},
            {"symbol": "CCCUSDT", "price_change_24h_pct": 7.0, "volume_24h_usd": 300},
        ])
        self.assertEqual(frame["نماد"].tolist(), ["CCCUSDT", "BBBUSDT", "AAAUSDT"])
        self.assertEqual(frame.iloc[0]["جهت بازار"], "🚀 در حال پامپ / صعود قوی")


if __name__ == "__main__":
    unittest.main()
