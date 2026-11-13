import unittest
from datetime import datetime, timezone, timedelta

from crypto_super_radar.feed_health import build_feed_health_report


class FeedHealthTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)

    def test_all_curated_symbols_fresh_is_live(self):
        board = {
            "status": "PASS",
            "updated_at_utc": self.now.isoformat(),
            "cache_ttl_seconds": 60,
            "total_items": 2,
            "sources": ["Binance Spot", "Bybit Spot"],
            "items": [{"status": "PASS"}, {"status": "PASS"}],
        }
        report = build_feed_health_report(board, now=self.now)
        self.assertEqual(report["status"], "LIVE")
        self.assertEqual(report["verified_symbols"], 2)

    def test_partial_coverage_is_not_live(self):
        board = {
            "status": "PARTIAL",
            "updated_at_utc": self.now.isoformat(),
            "cache_ttl_seconds": 60,
            "total_items": 2,
            "items": [{"status": "PASS"}, {"status": "UNKNOWN"}],
        }
        self.assertEqual(build_feed_health_report(board, now=self.now)["status"], "PARTIAL")

    def test_expired_cache_is_stale(self):
        board = {
            "status": "PASS",
            "updated_at_utc": (self.now - timedelta(seconds=61)).isoformat(),
            "cache_ttl_seconds": 60,
            "total_items": 1,
            "items": [{"status": "PASS"}],
        }
        self.assertEqual(build_feed_health_report(board, now=self.now)["status"], "STALE")

    def test_missing_timestamp_is_unknown(self):
        self.assertEqual(build_feed_health_report({"items": []}, now=self.now)["status"], "UNKNOWN")

    def test_future_timestamp_is_drift(self):
        board = {"updated_at_utc": (self.now + timedelta(minutes=2)).isoformat()}
        self.assertEqual(build_feed_health_report(board, now=self.now)["status"], "DRIFT")


if __name__ == "__main__":
    unittest.main()
