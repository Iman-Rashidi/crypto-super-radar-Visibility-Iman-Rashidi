"""Offline tests for the BloFin Demo Challenge Hunter."""
from datetime import datetime, timezone
import unittest

from crypto_super_radar.demo_challenge_hunter import (
    classify_campaign,
    scan_blofin_demo_challenges,
)


class DemoChallengeHunterTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 26, 3, 0, tzinfo=timezone.utc)

    def test_demo_only_campaign_is_candidate_not_guaranteed_eligible(self):
        item = classify_campaign(
            "Demo Trading Competition",
            "https://blofin.com/activity/demo",
            "https://blofin.com/activity/en",
            "Demo trading competition. Virtual account. Win rewards. Register now.",
            self.now,
        )
        self.assertEqual(item.demo_only_status, "POTENTIALLY_DEMO_ONLY")
        self.assertEqual(item.campaign_status, "POSSIBLY_ACTIVE")
        self.assertTrue(item.demo_evidence)
        self.assertFalse(item.live_requirement_evidence)

    def test_demo_to_live_is_not_demo_only(self):
        item = classify_campaign(
            "Demo to Live",
            "https://blofin.com/activity/demo-live",
            "https://blofin.com/activity/en",
            "Complete a Demo trade, deposit 100 USDT and make a live Futures trade.",
            self.now,
        )
        self.assertEqual(item.demo_only_status, "LIVE_REQUIREMENTS_FOUND")
        self.assertTrue(item.live_requirement_evidence)

    def test_unknown_text_fails_closed(self):
        item = classify_campaign(
            "Competition",
            "https://blofin.com/activity/competition",
            "https://blofin.com/activity/en",
            "Rewards are subject to terms.",
            self.now,
        )
        self.assertEqual(item.demo_only_status, "NOT_DEMO_CONFIRMED")
        self.assertEqual(item.campaign_status, "UNKNOWN")

    def test_scanner_uses_only_official_https_links_and_reports_read_only(self):
        pages = {
            "https://blofin.com/activity/en": (
                '<a href="/activity/competition/demo-1">Demo Trading Competition</a>'
                '<a href="https://evil.example/demo">Demo scam</a>'
                '<a href="http://blofin.com/demo">Insecure</a>'
            ),
            "https://blofin.com/activity/competition/demo-1": (
                "<h1>Demo Trading Competition</h1><p>Virtual account, demo trading, "
                "prize and register now.</p>"
            ),
        }
        report = scan_blofin_demo_challenges(
            lambda url: pages[url],
            now=self.now,
        )
        self.assertEqual(len(report["results"]), 1)
        self.assertFalse(report["orders_submitted"])
        self.assertFalse(report["registration_submitted"])
        self.assertEqual(report["mode"], "READ_ONLY_PUBLIC_DATA")

    def test_rejects_non_blofin_activity_source(self):
        with self.assertRaises(ValueError):
            scan_blofin_demo_challenges(
                lambda _url: "",
                activity_url="https://evil.example/activity",
                now=self.now,
            )


if __name__ == "__main__":
    unittest.main()
