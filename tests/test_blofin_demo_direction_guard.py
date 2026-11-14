import unittest

from crypto_super_radar.blofin_demo_executor import demo_direction_allowed


class DemoDirectionGuardTests(unittest.TestCase):
    def test_unrestricted_futures_mode_allows_long_and_short(self):
        self.assertTrue(demo_direction_allowed("LONG"))
        self.assertTrue(demo_direction_allowed("SHORT"))

    def test_buy_test_accepts_only_long(self):
        self.assertTrue(demo_direction_allowed("LONG", "LONG"))
        self.assertFalse(demo_direction_allowed("SHORT", "LONG"))

    def test_short_test_accepts_only_short(self):
        self.assertTrue(demo_direction_allowed("SHORT", "SHORT"))
        self.assertFalse(demo_direction_allowed("LONG", "SHORT"))

    def test_neutral_or_invalid_signal_fails_closed(self):
        self.assertFalse(demo_direction_allowed("NO_TRADE"))
        self.assertFalse(demo_direction_allowed("UNKNOWN"))
        self.assertFalse(demo_direction_allowed("", "LONG"))

    def test_invalid_required_direction_fails_closed(self):
        self.assertFalse(demo_direction_allowed("LONG", "BUY"))
        self.assertFalse(demo_direction_allowed("SHORT", "SELL"))


if __name__ == "__main__":
    unittest.main()
