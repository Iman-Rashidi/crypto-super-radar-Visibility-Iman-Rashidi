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
        parts = url.rstrip("/").split("/")
        network, endpoint = parts[-2], parts[-1]
        key = f"{network}_{endpoint}"
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
        sol_response = {
            "data": [self.sol_pool],
            "included": [
                {"id": f"solana_{SOL_TOKEN}", "type": "token",
                 "attributes": {"address": SOL_TOKEN, "name": "Sol Test", "symbol": "ST", "decimals": 9}},
                {"id": "test-dex", "type": "dex", "attributes": {"name": "Test DEX"}},
            ],
        }
        base_response = {
            "data": [self.base_pool],
            "included": [
                {"id": f"base_{BASE_TOKEN}", "type": "token",
                 "attributes": {"address": BASE_TOKEN, "name": "Base Test", "symbol": "BT", "decimals": 18}},
                {"id": "test-dex", "type": "dex", "attributes": {"name": "Test DEX"}},
            ],
        }
        empty = {"data": [], "included": []}
        self.responses = {
            "solana_new_pools": sol_response,
            "solana_trending_pools": empty,
            "base_new_pools": base_response,
            "base_trending_pools": empty,
        }

    def test_discovers_only_supported_chains_and_normalizes_evidence(self):
        http = FakeHttp(self.responses)
        result = discover_gem_candidates(http, limit=20, now=self.now)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["trade_authorization"], "NO_TRADE")
        self.assertEqual({item["chain"] for item in result["candidates"]}, {"solana", "base"})
        self.assertTrue(all(item["candidate_status"] == "UNSCREENED" for item in result["candidates"]))
        self.assertTrue(all(item["discovery_priority_score"] is not None for item in result["candidates"]))
        self.assertEqual(len(http.calls), 4)
        self.assertTrue(all(call[1]["include"] == "base_token,quote_token,dex" for call in http.calls))
        self.assertEqual(result["candidate_counts_by_network"], {"base": 1, "solana": 1})

    def test_deduplicates_token_across_sources_and_merges_provenance(self):
        trending = {
            "data": [self.sol_pool],
            "included": self.responses["solana_new_pools"]["included"],
        }
        responses = dict(self.responses)
        responses["solana_trending_pools"] = trending
        http = FakeHttp(responses)
        result = discover_gem_candidates(http, now=self.now)
        sol = next(item for item in result["candidates"] if item["chain"] == "solana")
        self.assertEqual(sol["sources"], ["GECKOTERMINAL_NEW_POOLS_SOLANA", "GECKOTERMINAL_TRENDING_POOLS_SOLANA"])
        self.assertEqual(result["candidate_count"], 2)

    def test_zero_liquidity_candidate_is_unknown_and_never_tradeable(self):
        zero = pool("solana", "PoolZero", f"solana_{SOL_TOKEN}", "2026-11-10T10:00:00Z", liquidity="0")
        response = {
            "data": [zero],
            "included": self.responses["solana_new_pools"]["included"],
        }
        responses = dict(self.responses)
        responses["solana_new_pools"] = response
        result = discover_gem_candidates(FakeHttp(responses), now=self.now)
        candidate = next(item for item in result["candidates"] if item["chain"] == "solana")
        self.assertEqual(candidate["candidate_status"], "UNKNOWN")
        self.assertIn("LIQUIDITY_UNAVAILABLE", candidate["risk_flags"])
        self.assertEqual(candidate["trade_authorization"], "NO_TRADE")

    def test_risk_adjustments_penalize_extreme_turnover_and_sharp_drawdown(self):
        risky_token = "7MYAKr4Tw2iBRNAqLneV68rn2amfpd7hWkjuh3f2pump"
        normal = pool("solana", "PoolNormal", f"solana_{SOL_TOKEN}", "2026-11-10T10:00:00Z",
                      liquidity="50000", volume="100000")
        risky = pool("solana", "PoolRisky", f"solana_{risky_token}", "2026-11-10T10:00:00Z",
                     liquidity="50000", volume="1000000")
        risky["attributes"]["price_change_percentage"]["h1"] = "-35"
        included = self.responses["solana_new_pools"]["included"] + [
            {"id": f"solana_{risky_token}", "type": "token",
             "attributes": {"address": risky_token, "name": "Risky", "symbol": "RISK", "decimals": 6}}
        ]
        response = {"data": [normal, risky], "included": included}
        responses = dict(self.responses)
        responses["solana_new_pools"] = response
        result = discover_gem_candidates(FakeHttp(responses), now=self.now)
        by_symbol = {item["token_symbol"]: item for item in result["candidates"]}
        self.assertEqual(len(by_symbol), 2)
        self.assertLess(by_symbol["RISK"]["discovery_priority_score"], by_symbol["ST"]["discovery_priority_score"])
        self.assertIn("SHARP_1H_DRAWDOWN", by_symbol["RISK"]["risk_flags"])
        self.assertIn("EXTREME_VOLUME_LIQUIDITY_RATIO", by_symbol["RISK"]["risk_flags"])
        self.assertEqual(by_symbol["RISK"]["trade_authorization"], "NO_TRADE")

    def test_rejects_invalid_token_address_and_unsupported_chain(self):
        invalid = pool("solana", "PoolBad", "solana_not-an-address", "2026-11-10T10:00:00Z")
        unsupported = pool("ethereum", "PoolEth", "ethereum_" + "0x" + "1" * 40, "2026-11-10T10:00:00Z")
        response = {"data": [invalid, unsupported], "included": []}
        responses = dict(self.responses)
        responses["solana_new_pools"] = response
        responses["base_new_pools"] = {"data": [], "included": []}
        result = discover_gem_candidates(FakeHttp(responses), now=self.now)
        self.assertEqual(result["candidate_count"], 0)

    def test_one_source_failure_is_partial_not_false_pass(self):
        responses = dict(self.responses)
        responses["base_trending_pools"] = RuntimeError("429")
        http = FakeHttp(responses)
        result = discover_gem_candidates(http, now=self.now)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["sources"]["GECKOTERMINAL_TRENDING_POOLS_BASE"]["status"], "UNKNOWN")
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
