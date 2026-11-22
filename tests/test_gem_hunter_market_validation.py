"""Tests for DEX Screener-primary exact-pool market validation."""
import unittest

import crypto_super_radar.gem_hunter_market_validation as validation
from crypto_super_radar.gem_hunter_market_validation import (
    reconcile_candidate_market_data,
    reconcile_discovery_candidates,
)


TOKEN = "0x" + "1" * 40
POOL = "0x" + "2" * 40
WETH = "0x4200000000000000000000000000000000000006"


def candidate(liquidity=100000, volume=200000):
    return {
        "chain": "base", "token_address": TOKEN, "pool_address": POOL,
        "liquidity_usd": liquidity, "volume_24h_usd": volume,
        "price_usd": 0.01, "discovery_priority_score": 50,
    }


class DexOnlyHttp:
    def __init__(self, *, liquidity=100000, volume=200000, price="0.01",
                 pool=POOL, fail=False):
        self.liquidity, self.volume, self.price, self.pool, self.fail = liquidity, volume, price, pool, fail
        self.calls = []

    def get(self, url, params=None):
        self.calls.append((url, params))
        if "geckoterminal" in url:
            raise AssertionError("GeckoTerminal is not part of the primary validation path")
        if "/tokens/v1/" in url:
            if self.fail:
                raise RuntimeError("HTTP 429")
            return [{
                "chainId": "base", "pairAddress": self.pool,
                "baseToken": {"address": TOKEN, "name": "Test", "symbol": "TST"},
                "quoteToken": {"address": WETH, "name": "WETH", "symbol": "WETH"},
                "liquidity": {"usd": self.liquidity}, "volume": {"h24": self.volume},
                "priceUsd": self.price, "priceNative": "1", "dexId": "uniswap",
            }]
        if "/token-pairs/v1/" not in url:
            raise AssertionError(url)
        if self.fail:
            raise RuntimeError("HTTP 429")
        return [{
            "chainId": "base", "pairAddress": self.pool,
            "baseToken": {"address": TOKEN, "name": "Test", "symbol": "TST"},
            "quoteToken": {"address": WETH, "name": "WETH", "symbol": "WETH"},
            "liquidity": {"usd": self.liquidity}, "volume": {"h24": self.volume},
            "priceUsd": self.price, "priceNative": "1", "dexId": "uniswap",
        }]


class GemHunterMarketValidationTests(unittest.TestCase):
    def setUp(self):
        validation._CACHE.clear()
        validation._SCAN_OFFSET = 0

    def test_primary_exact_pool_match_passes_with_explicit_single_source_label(self):
        http = DexOnlyHttp()
        result = reconcile_candidate_market_data(candidate(), http)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["source_count"], 1)
        self.assertEqual(result["verification_level"], "DEXSCREENER_PRIMARY_SINGLE_SOURCE")
        self.assertEqual(result["independent_confirmation"], "NOT_PERFORMED")
        self.assertEqual(result["verified_liquidity_usd"], 100000)
        self.assertEqual(result["verified_volume_24h_usd"], 200000)
        self.assertEqual(len(http.calls), 1)

    def test_gecko_429_or_unavailability_cannot_block_primary_validation(self):
        http = DexOnlyHttp()
        result = reconcile_candidate_market_data(candidate(), http)
        self.assertEqual(result["status"], "PASS")
        self.assertFalse(any("geckoterminal" in url for url, _ in http.calls))

    def test_primary_429_fails_closed_without_gecko_fallback(self):
        http = DexOnlyHttp(fail=True)
        result = reconcile_candidate_market_data(candidate(), http)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertIn("DEXSCREENER_PRIMARY_UNAVAILABLE", result["reason_codes"])
        self.assertFalse(any("geckoterminal" in url for url, _ in http.calls))

    def test_primary_liquidity_below_floor_fails(self):
        result = reconcile_candidate_market_data(
            candidate(100, 100), DexOnlyHttp(liquidity=100, volume=100)
        )
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("PRIMARY_LIQUIDITY_BELOW_FLOOR", result["reason_codes"])

    def test_discovery_metrics_mismatch_fails_closed(self):
        result = reconcile_candidate_market_data(
            candidate(190000, 200000), DexOnlyHttp(liquidity=100000, volume=200000)
        )
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("MARKET_LIQUIDITY_MISMATCH", result["reason_codes"])

    def test_exact_pool_identity_mismatch_is_unknown_not_substituted(self):
        result = reconcile_candidate_market_data(
            candidate(), DexOnlyHttp(pool="0x" + "3" * 40)
        )
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["source_count"], 0)

    def test_evm_addresses_are_case_insensitive_but_solana_base58_is_not(self):
        self.assertTrue(validation._same_address("0xAbCd", "0xabcd", "base"))
        self.assertFalse(validation._same_address("AbCd", "abcd", "solana"))

    def test_dexscreener_quote_side_price_is_derived_from_base_usd_and_native_ratio(self):
        class QuoteSideHttp(DexOnlyHttp):
            def get(inner, url, params=None):
                inner.calls.append((url, params))
                return [{
                    "chainId": "base", "pairAddress": POOL,
                    "baseToken": {"address": WETH, "name": "WETH", "symbol": "WETH"},
                    "quoteToken": {"address": TOKEN, "name": "Test", "symbol": "TST"},
                    "liquidity": {"usd": 100000}, "volume": {"h24": 200000},
                    "priceUsd": "2000", "priceNative": "2000", "dexId": "uniswap",
                }]
        snapshot = validation._dex_snapshot(QuoteSideHttp(), "base", TOKEN, POOL)
        self.assertAlmostEqual(snapshot["price_usd"], 1.0)
        self.assertEqual(snapshot["price_source"], "DERIVED_QUOTE_PRICE_USD")

    def test_scan_budget_rotates_to_candidates_beyond_first_batch(self):
        rows = [candidate(), dict(candidate(), token_address="0x" + "4" * 40,
                                   pool_address="0x" + "5" * 40)]
        first = reconcile_discovery_candidates(rows, DexOnlyHttp(), limit=1)
        self.assertEqual(first[0]["market_reconciliation"]["status"], "PASS")
        self.assertEqual(first[1]["market_reconciliation"]["status"], "NOT_CHECKED_BUDGET")
        second = reconcile_discovery_candidates(rows, DexOnlyHttp(), limit=1)
        self.assertNotEqual(second[1]["market_reconciliation"]["status"], "NOT_CHECKED_BUDGET")


if __name__ == "__main__":
    unittest.main()
