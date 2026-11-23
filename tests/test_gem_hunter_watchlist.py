"""Offline tests for the permanent €100 Gem Hunter watchlist ledger."""
import sqlite3
import unittest
from unittest.mock import Mock

from crypto_super_radar.gem_hunter_watchlist import (
    ensure_watchlist_schema, record_watchlist_candidates, refresh_watchlist_prices,
    watchlist_snapshot, is_watchlist_eligible,
)

class WatchlistLedgerTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        ensure_watchlist_schema(self.db)

    def tearDown(self):
        self.db.close()

    def candidate(self, **overrides):
        row = {"chain":"solana","token_address":"MintABC","token_name":"Alpha",
               "token_symbol":"ALP","price_usd":0.01,"category_fa":"میم‌کوین",
               "gem_hunter_v3_status":"TIER_2_EARLY_ALPHA"}
        row.update(overrides)
        return row

    def test_first_detection_name_timestamp_and_initial_price_are_immutable(self):
        record_watchlist_candidates(self.db, [self.candidate()])
        first = dict(self.db.execute("SELECT * FROM gem_hunter_watchlist_ledger").fetchone())
        record_watchlist_candidates(self.db, [self.candidate(token_name="Renamed", token_symbol="NEW", price_usd=0.02)])
        second = dict(self.db.execute("SELECT * FROM gem_hunter_watchlist_ledger").fetchone())
        self.assertEqual(second["first_detected_utc"], first["first_detected_utc"])
        self.assertEqual(second["initial_price_usd"], 0.01)
        self.assertEqual(second["token_name"], "Alpha")
        self.assertEqual(second["token_symbol"], "ALP")
        self.assertEqual(second["detection_count"], 2)

    def test_eur_100_current_and_peak_profit_calculation(self):
        record_watchlist_candidates(self.db, [self.candidate()])
        class FakeResponse:
            def raise_for_status(self): pass
            def json(self):
                return {"pairs":[{"chainId":"solana","baseToken":{"address":"MintABC"},
                                  "priceUsd":"0.025","liquidity":{"usd":100000}}]}
        client = Mock()
        client.get.return_value = FakeResponse()
        result = refresh_watchlist_prices(self.db, session=client)
        self.assertEqual(result["prices_updated"], 1)
        item = watchlist_snapshot(self.db)["items"][0]
        self.assertAlmostEqual(item["current_change_pct"], 150.0)
        self.assertAlmostEqual(item["eur_100_current_value"], 250.0)
        self.assertAlmostEqual(item["eur_100_net_profit_eur"], 150.0)
        self.assertAlmostEqual(item["peak_change_pct"], 150.0)
        self.assertAlmostEqual(item["eur_100_peak_profit_eur"], 150.0)

    def test_early_alpha_requires_rising_phase_and_52_percent_buy_share(self):
        self.assertTrue(is_watchlist_eligible(self.candidate(gem_hunter_v3_status="EARLY_ALPHA_REVIEW",
            pump_phase_fa="🟢 فاز ۱: شروع پامپ (Early Ignition)", buy_volume_share_pct=52)))
        self.assertFalse(is_watchlist_eligible(self.candidate(gem_hunter_v3_status="EARLY_ALPHA_REVIEW",
            pump_phase_fa="🔴 فاز ۳: سقف فرسودگی / خطر دامپ", buy_volume_share_pct=90)))
        self.assertFalse(is_watchlist_eligible(self.candidate(gem_hunter_v3_status="EARLY_ALPHA_REVIEW",
            pump_phase_fa="🟡 فاز ۲: میانه موج صعودی", buy_volume_share_pct=51.99)))

if __name__ == "__main__":
    unittest.main()
