"""Tests for DEX Screener-primary Gem Hunter discovery."""
from datetime import datetime, timezone
import unittest

from crypto_super_radar import gem_hunter_discovery as discovery
from crypto_super_radar.gem_hunter_discovery import discover_gem_candidates


NOW = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)


def token_address(chain, index):
    if chain == "solana":
        alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
        return "7" + alphabet[index] + "A" * 32
    return f"0x{index + 1:040x}"


def pair(chain, token, index):
    pool = ("9" + "A" * 32) if chain == "solana" else f"0x{index + 100:040x}"
    quote = (
        "So11111111111111111111111111111111111111112" if chain == "solana"
        else "0x4200000000000000000000000000000000000006"
    )
    return {
        "chainId": chain, "pairAddress": pool,
        "baseToken": {"address": token, "name": f"Token {index}", "symbol": f"T{index}"},
        "quoteToken": {"address": quote, "name": "Wrapped Native", "symbol": "WN"},
        "priceUsd": "0.025", "priceNative": "0.00001",
        "liquidity": {"usd": 150000}, "volume": {"h1": 25000, "h24": 350000},
        "txns": {"h1": {"buys": 100, "sells": 40}, "h24": {"buys": 500, "sells": 200}},
        "pairCreatedAt": int((NOW.timestamp() - 7200) * 1000),
        "priceChange": {"h1": 8.5}, "dexId": "test-dex",
    }


class DexOnlyHttp:
    def __init__(self):
        self.calls = []
        self.rows = {}
        index = 0
        for chain in ("solana", "base", "ethereum"):
            for _ in range(4):
                token = token_address(chain, index)
                self.rows[(chain, token)] = pair(chain, token, index)
                index += 1

    def get(self, url, params=None):
        self.calls.append((url, params))
        if url.endswith("/token-boosts/latest/v1"):
            return [
                {"chainId": chain, "tokenAddress": token}
                for chain, token in self.rows
            ]
        if url.endswith("/token-profiles/latest/v1"):
            return [
                {"chainId": chain, "tokenAddress": token}
                for chain, token in self.rows
            ]
        if "/latest/dex/search?" in url:
            return {"pairs": [row for (chain, _), row in self.rows.items() if chain in {"base", "ethereum"}]}
        if "/latest/dex/tokens/" in url:
            _, addresses = url.split("/latest/dex/tokens/", 1)
            wanted = set(addresses.split(","))
            return {"pairs": [row for (network, token), row in self.rows.items()
                    if token in wanted]}
        if "/token-pairs/v1/" in url:
            _, tail = url.split("/token-pairs/v1/", 1)
            chain, token = tail.split("/", 1)
            row = self.rows.get((chain, token))
            return [row] if row else []
        if "geckoterminal" in url:
            raise AssertionError("GeckoTerminal must not be called on the primary path")
        raise AssertionError(f"Unexpected URL: {url}")


class LatestSolanaOnlyHttp(DexOnlyHttp):
    def get(self, url, params=None):
        if url.endswith("/token-boosts/latest/v1") or url.endswith("/token-profiles/latest/v1"):
            self.calls.append((url, params))
            return [
                {"chainId": chain, "tokenAddress": token}
                for chain, token in self.rows if chain == "solana"
            ]
        return super().get(url, params)


class GemHunterDexDiscoveryTests(unittest.TestCase):
    def setUp(self):
        discovery._DEXSCREENER_SOURCE_CACHE.clear()
        discovery._GECKOTERMINAL_SOURCE_CACHE.clear()

    def test_dexscreener_discovers_all_three_chains_with_batch_endpoint(self):
        http = DexOnlyHttp()
        result = discover_gem_candidates(http, limit=50, now=NOW)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["primary_source"], "DEXSCREENER")
        self.assertEqual(result["trade_authorization"], "NO_TRADE")
        self.assertEqual(set(result["candidate_counts_by_network"]), {"solana", "base", "ethereum"})
        self.assertEqual(result["candidate_count"], 12)
        batch_calls = [url for url, _ in http.calls if "/latest/dex/tokens/" in url]
        self.assertEqual(len(batch_calls), 3)
        self.assertTrue(all("GECKOTERMINAL" not in str(item) for item in result["sources"]))
        self.assertTrue(all(row["source"] == "DEXSCREENER_PRIMARY" for row in result["candidates"]))
        self.assertTrue(all(row["trade_authorization"] == "NO_TRADE" for row in result["candidates"]))

    def test_search_supplements_missing_base_and_ethereum_latest_feeds(self):
        http = LatestSolanaOnlyHttp()
        result = discover_gem_candidates(http, limit=50, now=NOW)
        counts = result["candidate_counts_by_network"]
        self.assertGreater(counts["solana"], 0)
        self.assertGreater(counts["base"], 0)
        self.assertGreater(counts["ethereum"], 0)
        self.assertTrue(any("/latest/dex/search?" in url for url, _ in http.calls))
        self.assertEqual(result["primary_source"], "DEXSCREENER")
        self.assertEqual(result["trade_authorization"], "NO_TRADE")

    def test_dexscreener_batch_size_never_exceeds_thirty(self):
        http = DexOnlyHttp()
        # Duplicate/large feed coverage is still bounded and split into batches of 30.
        http.rows = {
            (chain, token_address(chain, index)): pair(chain, token_address(chain, index), index)
            for index, chain in enumerate(["solana"] * 35)
        }
        result = discover_gem_candidates(http, limit=50, now=NOW)
        batch_calls = [url for url, _ in http.calls if "/latest/dex/tokens/" in url]
        self.assertEqual(len(batch_calls), 2)
        self.assertTrue(all(len(url.rsplit("/", 1)[-1].split(",")) <= 30 for url in batch_calls))
        self.assertGreater(result["candidate_count"], 0)

    def test_gecko_429_cannot_block_discovery_because_it_is_not_called(self):
        http = DexOnlyHttp()
        result = discover_gem_candidates(http, limit=10, now=NOW)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["candidate_count"], 10)
        self.assertFalse(any("geckoterminal" in url for url, _ in http.calls))

    def test_invalid_http_client_fails_closed(self):
        result = discover_gem_candidates(None, now=NOW)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["candidate_count"], 0)
        self.assertEqual(result["trade_authorization"], "NO_TRADE")


if __name__ == "__main__":
    unittest.main()
