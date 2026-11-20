import unittest

from crypto_super_radar.market_replay import (
    ReplayDownloadError,
    _record_unique_timestamp,
    _parse_funding_event,
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


class ReplayFundingSchemaTests(unittest.TestCase):
    def test_missing_funding_kind_is_not_fabricated_as_regular(self):
        with self.assertRaisesRegex(ReplayDownloadError, "missing or unsupported fundingKind"):
            _parse_funding_event(
                {"fundingTime": "1000", "fundingRate": "0.0001"},
                symbol="BTC-USDT",
            )

    def test_unknown_funding_kind_is_rejected(self):
        with self.assertRaisesRegex(ReplayDownloadError, "missing or unsupported fundingKind"):
            _parse_funding_event(
                {"fundingTime": "1000", "fundingRate": "0.0001", "fundingKind": "9"},
                symbol="BTC-USDT",
            )

    def test_regular_and_dividend_funding_kinds_are_preserved(self):
        regular = _parse_funding_event(
            {"fundingTime": "1000", "fundingRate": "0.0001", "fundingKind": "0"},
            symbol="BTC-USDT",
        )
        dividend = _parse_funding_event(
            {"fundingTime": "2000", "fundingRate": "0.0002", "fundingKind": "1"},
            symbol="BTC-USDT",
        )
        self.assertEqual(regular, ("BTC-USDT", 1000, 0.0001, "0"))
        self.assertEqual(dividend, ("BTC-USDT", 2000, 0.0002, "1"))


if __name__ == "__main__":
    unittest.main()
