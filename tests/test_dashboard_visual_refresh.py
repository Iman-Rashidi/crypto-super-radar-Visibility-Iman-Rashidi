from pathlib import Path
import unittest


class DashboardVisualRefreshTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = Path("crypto_super_radar/web_app.py").read_text(encoding="utf-8")

    def test_modern_radar_visual_theme_and_mobile_navigation(self):
        source = self.source
        self.assertIn("backdrop-filter:blur(12px)", source)
        self.assertIn("Vazirmatn", source)
        self.assertIn("radar-theme-toggle", source)
        self.assertIn("light-theme", source)
        self.assertIn("CRYPTO SUPER RADAR", source)
        self.assertIn("LIVE PULSE", source)
        self.assertIn("radar-quick-jump", source)
        for anchor in ("#gem-hunter-panel", "#free-airdrop-radar", "#futures-paper-bot", "#live-market-radar"):
            with self.subTest(anchor=anchor):
                self.assertIn(anchor, source)

    def test_scan_radar_motion_and_reduced_motion_accessibility(self):
        source = self.source
        for marker in ("radar-sweep", "radar-wave", "radar-progress", "is-active",
                       "prefers-reduced-motion:reduce", "gem-hunter-run",
                       "اجرای اسکن زنده Gem Hunter V3"):
            with self.subTest(marker=marker):
                self.assertIn(marker, source)

    def test_gem_candidate_visual_scores_badges_and_copy_feedback(self):
        source = self.source
        for marker in ("gem-score-panel", "gem-score-fill opportunity",
                       "gem-score-fill risk", "gem-visual-badges",
                       "net-solana", "net-base", "net-ethereum",
                       "gem-copy-success", "کپی آدرس قرارداد"):
            with self.subTest(marker=marker):
                self.assertIn(marker, source)

    def test_existing_dashboard_controls_and_safety_contracts_remain(self):
        source = self.source
        for existing_id in (
            "gem-hunter-run", "gem-hunter-results", "free-airdrop-radar",
            "futures-paper-bot", "live-market-radar", "live-radar-refresh",
            "crypto-market-refresh", "scan-asset",
        ):
            with self.subTest(element_id=existing_id):
                self.assertIn(f'id="{existing_id}"', source)
        self.assertIn('"NO_TRADE"', source)
        self.assertIn("CHALLENGE_1_V2", source)


if __name__ == "__main__":
    unittest.main()
