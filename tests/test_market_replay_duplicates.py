import unittest

from crypto_super_radar.market_replay import (
    ReplayDownloadError,
    _record_unique_timestamp,
)


class ReplayDuplicateTimestampTests(unittest.TestCase):
    def test_first_timestamp_is_recorded(self):
        seen = {}
        row = ("BTC-USDT", "15m", 1000)
        _record_unique_timestamp(seen, 1000, row, series="BTC-USDT 15m")
        self.assertEqual(seen, {1000: row})

    def test_duplicate_candle_timestamp_fails_without_overwrite(self):
        seen = {}
        first = ("BTC-USDT", "15m", 1000, "first")
        second = ("BTC-USDT", "15m", 1000, "second")
        _record_unique_timestamp(seen, 1000, first, series="BTC-USDT 15m")
        with self.assertRaisesRegex(ReplayDownloadError, "duplicate timestamp"):
            _record_unique_timestamp(seen, 1000, second, series="BTC-USDT 15m")
        self.assertEqual(seen[1000], first)

    def test_duplicate_funding_timestamp_fails(self):
        seen = {}
        row = ("BTC-USDT", 2000, 0.0001, "0")
        _record_unique_timestamp(seen, 2000, row, series="BTC-USDT funding")
        with self.assertRaisesRegex(ReplayDownloadError, "BTC-USDT funding"):
            _record_unique_timestamp(seen, 2000, row, series="BTC-USDT funding")


if __name__ == "__main__":
    unittest.main()
