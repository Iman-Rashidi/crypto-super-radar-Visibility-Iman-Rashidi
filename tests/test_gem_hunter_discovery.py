"""Tests for conservative Gem Hunter candidate discovery."""
from datetime import datetime, timezone
import unittest

from crypto_super_radar.gem_hunter_discovery import discover_gem_candidates


SOL_TOKEN = "61iusjsjCqfCW6SViHytSyYqvALUMcv3k2xMsUtM1UwP"
BASE_TOKEN = "0x1111111111111111111111111111111111111111"


def pool(chain, address, token_id, created, liquidity="50000", volume="100000"):
    return {
        "id": f"{chain}_{address}",
        "type": "pool",
        "attributes": {
            "address": address,
            "name": "TEST / USD",
            "pool_created_at": created,
            "base_token_price_usd": "0.01",
            "reserve_in_usd": liquidity,
            "volume_usd": {"h1": "12000", "h24": volume},
            "transactions": {"h1": {"buys": 80, "sells": 20, "buyers": 45, "sellers": 18},
                            "h24": {"buys": 200, "sells": 80}},
            "price_change_percentage": {"h1": "8.5"},
        },
        "relationships": {
            "base_token": {"data": {"id": token_id, "type": "token"}},
            "quote_token": {"data": {"id": f"{chain}_quote", "type": "token"}},
            "dex": {"data": {"id": "test-dex", "type": "dex"}},
        },
    }


class FakeHttp:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, url, params=None):
        self.calls.append((url, params))
        key = "new_pools" if url.endswith("/new_pools") else "trending_pools"
        value = self.responses[key]
        if isinstance(value, Exception):
            raise value
        return value


class GemHunterDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
        self.sol_pool = pool(
            "solana", "PoolSol", f"solana_{SOL_TOKEN}",
            "2026-11-10T10:00:00Z",
        )
        self.base_pool = pool(
            "base", "0x" + "2" * 40, f"base_{BASE_TOKEN}",
            "2026-11-09T12:00:00Z",
        )
        self.responses = {
            "new_pools": {
                "data": [self.sol_pool, self.base_pool],
                "included": [
                    {"id": f"solana_{SOL_TOKEN}", "type": "token",
                     "attributes": {"address": SOL_TOKEN, "name": "Sol Test", "symbol": "ST", "decimals": 9}},
                    {"id": f"base_{BASE_TOKEN}", "type": "token",
                     "attributes": {"address": BASE_TOKEN, "name": "Base Test", "symbol": "BT", "decimals": 18}},
                    {"id": "test-dex", "type": "dex", "attributes": {"name": "Test DEX"}},
                ],
            },
            "trending_pools": {"data": [], "included": []},
        }

    def test_discovers_only_supported_chains_and_normalizes_evidence(self):
        http = FakeHttp(self.responses)
        result = discover_gem_candidates(http, limit=20, now=self.now)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["trade_authorization"], "NO_TRADE")
        self.assertEqual({item["chain"] for item in result["candidates"]}, {"solana", "base"})
        self.assertTrue(all(item["candidate_status"] == "UNSCREENED" for item in result["candidates"]))
        self.assertTrue(all(item["discovery_priority_score"] is not None for item in result["candidates"]))
        self.assertEqual(len(http.calls), 2)
        self.assertEqual(http.calls[0][1]["include"], "base_token,quote_token,dex")

    def test_deduplicates_token_across_sources_and_merges_provenance(self):
        trending = {
            "data": [self.sol_pool],
            "included": self.responses["new_pools"]["included"],
        }
        http = FakeHttp({"new_pools": self.responses["new_pools"], "trending_pools": trending})
        result = discover_gem_candidates(http, now=self.now)
        sol = next(item for item in result["candidates"] if item["chain"] == "solana")
        self.assertEqual(sol["sources"], ["GECKOTERMINAL_NEW_POOLS", "GECKOTERMINAL_TRENDING_POOLS"])
        self.assertEqual(result["candidate_count"], 2)

    def test_zero_liquidity_candidate_is_unknown_and_never_tradeable(self):
        zero = pool("solana", "PoolZero", f"solana_{SOL_TOKEN}", "2026-11-10T10:00:00Z", liquidity="0")
        response = {
            "data": [zero],
            "included": self.responses["new_pools"]["included"],
        }
        result = discover_gem_candidates(FakeHttp({"new_pools": response, "trending_pools": {"data": []}}), now=self.now)
        candidate = result["candidates"][0]
        self.assertEqual(candidate["candidate_status"], "UNKNOWN")
        self.assertIn("LIQUIDITY_UNAVAILABLE", candidate["risk_flags"])
        self.assertEqual(candidate["trade_authorization"], "NO_TRADE")

    def test_rejects_invalid_token_address_and_unsupported_chain(self):
        invalid = pool("solana", "PoolBad", "solana_not-an-address", "2026-11-10T10:00:00Z")
        unsupported = pool("ethereum", "PoolEth", "ethereum_" + "0x" + "1" * 40, "2026-11-10T10:00:00Z")
        response = {"data": [invalid, unsupported], "included": []}
        result = discover_gem_candidates(FakeHttp({"new_pools": response, "trending_pools": {"data": []}}), now=self.now)
        self.assertEqual(result["candidate_count"], 0)

    def test_one_source_failure_is_partial_not_false_pass(self):
        http = FakeHttp({"new_pools": self.responses["new_pools"], "trending_pools": RuntimeError("429")})
        result = discover_gem_candidates(http, now=self.now)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["sources"]["GECKOTERMINAL_TRENDING_POOLS"]["status"], "UNKNOWN")
        self.assertEqual(result["trade_authorization"], "NO_TRADE")

    def test_unavailable_http_client_returns_unknown(self):
        result = discover_gem_candidates(None, now=self.now)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["candidate_count"], 0)
        self.assertEqual(result["trade_authorization"], "NO_TRADE")

    def test_limit_is_bounded(self):
        result = discover_gem_candidates(FakeHttp(self.responses), limit=1000, now=self.now)
        self.assertLessEqual(result["candidate_count"], 50)


if __name__ == "__main__":
    unittest.main()
