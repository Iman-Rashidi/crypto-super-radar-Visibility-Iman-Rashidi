"""Tests for exact-pool cross-source market-data reconciliation."""
import unittest

import crypto_super_radar.gem_hunter_market_validation as validation
from crypto_super_radar.gem_hunter_market_validation import (
    reconcile_candidate_market_data,
    reconcile_discovery_candidates,
)


TOKEN = "0x" + "1" * 40
POOL = "0x" + "2" * 40
WETH = "0x4200000000000000000000000000000000000006"


class FakeHttp:
    def __init__(self, *, dex_liquidity=100000, gecko_liquidity=100000,
                 dex_volume=200000, gecko_volume=200000, dex_pool=POOL):
        self.dex_liquidity = dex_liquidity
        self.gecko_liquidity = gecko_liquidity
        self.dex_volume = dex_volume
        self.gecko_volume = gecko_volume
        self.dex_pool = dex_pool
        self.calls = []

    def get(self, url, params=None):
        self.calls.append((url, params))
        if "token-pairs" in url:
            return [{
                "chainId": "base",
                "pairAddress": self.dex_pool,
                "baseToken": {"address": TOKEN, "name": "Test", "symbol": "TST"},
                "quoteToken": {"address": WETH, "name": "WETH", "symbol": "WETH"},
                "liquidity": {"usd": str(self.dex_liquidity)},
                "volume": {"h24": str(self.dex_volume)},
                "priceUsd": "0.01",
                "dexId": "uniswap",
            }]
        return {
            "data": {
                "id": "base_" + POOL,
                "attributes": {
                    "address": POOL,
                    "reserve_in_usd": str(self.gecko_liquidity),
                    "volume_usd": {"h24": str(self.gecko_volume)},
                    "base_token_price_usd": "0.01",
                },
                "relationships": {
                    "base_token": {"data": {"id": "base_token"}},
                    "quote_token": {"data": {"id": "base_quote"}},
                },
            },
            "included": [
                {"id": "base_token", "attributes": {"address": TOKEN}},
                {"id": "base_quote", "attributes": {"address": WETH}},
            ],
        }


def candidate(liquidity=100000, volume=200000):
    return {
        "chain": "base",
        "token_address": TOKEN,
        "pool_address": POOL,
        "liquidity_usd": liquidity,
        "volume_24h_usd": volume,
        "discovery_priority_score": 50,
    }


class GemHunterMarketValidationTests(unittest.TestCase):
    def setUp(self):
        validation._CACHE.clear()
        validation._SCAN_OFFSET = 0

    def test_evm_addresses_are_case_insensitive_but_solana_base58_is_not(self):\n        self.assertTrue(validation._same_address("0xAbCd", "0xabcd", "base"))\n        self.assertFalse(validation._same_address("AbCd", "abcd", "solana"))\n\n    def test_matching_exact_pool_sources_pass(self):
        result = reconcile_candidate_market_data(candidate(), FakeHttp())
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["source_count"], 2)
        self.assertEqual(result["verified_liquidity_usd"], 100000)
        self.assertEqual(result["verified_volume_24h_usd"], 200000)

    def test_discovery_liquidity_conflict_fails_closed(self):
        # Reproduces the critical CLAUDIA-class defect: discovery says $190k,
        # while both exact-pool sources report effectively no liquidity.
        http = FakeHttp(dex_liquidity=0.0004, gecko_liquidity=0.000436)
        result = reconcile_candidate_market_data(candidate(190295, 13761), http)
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("MARKET_DATA_DISAGREEMENT", result["reason_codes"])
        self.assertLess(result["verified_liquidity_usd"], 1)

    def test_sources_that_disagree_fail(self):
        result = reconcile_candidate_market_data(
            candidate(), FakeHttp(dex_liquidity=100000, gecko_liquidity=1000)
        )
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("MARKET_DATA_DISAGREEMENT", result["reason_codes"])

    def test_exact_pool_identity_mismatch_is_unknown_not_substituted(self):
        result = reconcile_candidate_market_data(
            candidate(), FakeHttp(dex_pool="0x" + "3" * 40)
        )
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["source_count"], 1)

    def test_matching_sources_below_liquidity_floor_fail(self):
        result = reconcile_candidate_market_data(
            candidate(100, 100), FakeHttp(dex_liquidity=100, gecko_liquidity=100,
                                           dex_volume=100, gecko_volume=100)
        )
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("VERIFIED_LIQUIDITY_BELOW_FLOOR", result["reason_codes"])

    def test_scan_budget_rotates_to_candidates_beyond_first_batch(self):
        validation._SCAN_OFFSET = 0
        rows = [candidate(), dict(candidate(), token_address="0x" + "4" * 40)]
        first = reconcile_discovery_candidates(rows, FakeHttp(), limit=1)
        self.assertEqual(first[0]["market_reconciliation"]["status"], "PASS")
        self.assertEqual(first[1]["market_reconciliation"]["status"], "NOT_CHECKED_BUDGET")
        second = reconcile_discovery_candidates(rows, FakeHttp(), limit=1)
        self.assertNotEqual(second[1]["market_reconciliation"]["status"], "NOT_CHECKED_BUDGET")

    def test_scan_budget_is_explicit_and_bounded(self):
        rows = [candidate(), dict(candidate(), token_address="0x" + "4" * 40)]
        result = reconcile_discovery_candidates(rows, FakeHttp(), limit=1)
        self.assertEqual(result[0]["market_reconciliation"]["status"], "PASS")
        self.assertEqual(result[1]["market_reconciliation"]["status"], "NOT_CHECKED_BUDGET")


if __name__ == "__main__":
    unittest.main()
