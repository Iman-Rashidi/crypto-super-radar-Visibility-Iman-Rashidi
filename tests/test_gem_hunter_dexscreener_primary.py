"""End-to-end proof that DEX Screener alone completes a ten-candidate Gem Hunter pass."""
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from crypto_super_radar import gem_hunter_discovery as discovery
from crypto_super_radar import gem_hunter_market_validation as market
from crypto_super_radar import web_app


NOW = datetime.now(timezone.utc)


def addr(chain, index):
    if chain == "solana":
        alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
        return "7" + alphabet[index] + "A" * 32
    return f"0x{index + 1:040x}"


class DexOnlyHttp:
    def __init__(self):
        self.calls = []
        self.pairs = {}
        index = 0
        for chain in ("solana", "base", "ethereum"):
            for _ in range(4):
                token = addr(chain, index)
                pool = ("9" + "B" * 32) if chain == "solana" else f"0x{index + 101:040x}"
                quote = "So11111111111111111111111111111111111111112" if chain == "solana" else "0x4200000000000000000000000000000000000006"
                self.pairs[(chain, token)] = {
                    "chainId": chain, "pairAddress": pool,
                    "baseToken": {"address": token, "name": f"Candidate {index}", "symbol": f"C{index}"},
                    "quoteToken": {"address": quote, "name": "Wrapped Native", "symbol": "WN"},
                    "priceUsd": "0.025", "priceNative": "0.00001",
                    "liquidity": {"usd": 150000}, "volume": {"h1": 25000, "h24": 350000},
                    "txns": {"h1": {"buys": 100, "sells": 40}, "h24": {"buys": 500, "sells": 200}},
                    "pairCreatedAt": int((NOW.timestamp() - 7200) * 1000),
                    "priceChange": {"h1": 8.5}, "dexId": "test-dex",
                }
                index += 1

    def get(self, url, params=None):
        self.calls.append((url, params))
        if "geckoterminal" in url:
            raise AssertionError("GeckoTerminal must not be needed for this pipeline")
        if url.endswith("/token-boosts/latest/v1") or url.endswith("/token-profiles/latest/v1"):
            return [{"chainId": chain, "tokenAddress": token} for chain, token in self.pairs]
        if "/latest/dex/tokens/" in url:
            addresses = url.split("/latest/dex/tokens/", 1)[1]
            wanted = set(addresses.split(","))
            return {"pairs": [pair for (network, token), pair in self.pairs.items()
                    if token in wanted]}

        raise AssertionError(url)


class DexScreenerOnlyPipelineTests(unittest.TestCase):
    def setUp(self):
        discovery._DEXSCREENER_SOURCE_CACHE.clear()
        discovery._GECKOTERMINAL_SOURCE_CACHE.clear()
        market._CACHE.clear()
        market._SCAN_OFFSET = 0
        with web_app.GEM_HUNTER_CACHE_LOCK:
            web_app.GEM_HUNTER_CACHE.update({"expires_at": 0.0, "payload": None})

    def test_ten_candidates_complete_without_geckoterminal_and_remain_no_trade(self):
        http = DexOnlyHttp()

        def screen(rows, data_service, limit=10):
            for row in rows:
                row["security_evidence"] = {
                    "status": "PASS", "mint_authority_active": False,
                    "freeze_authority_active": False, "honeypot_detected": False,
                    "sell_tax_pct": 0, "secondary_holder_data": "NOT_AVAILABLE",
                }
            return rows

        def evaluate(rows):
            for row in rows:
                row["qualification"] = {
                    "schema_version": web_app.SCHEMA_VERSION,
                    "decision": "NO_TRADE", "market_type": "SPOT",
                    "filters": {key: "UNKNOWN" for key in web_app.FILTER_IDS},
                    "critical_gates": {key: "UNKNOWN" for key in web_app.CRITICAL_GATES},
                    "market_metrics": {}, "data_as_of_utc": NOW.isoformat(),
                    "market_data_sources": ["DEXSCREENER"],
                    "whale_evidence": {"wallet_breadth": "NOT_AVAILABLE"},
                }
            return rows

        def process(rows, notify=True):
            for index, row in enumerate(rows):
                row["gem_hunter_v3_status"] = (
                    "TIER_2_EARLY_ALPHA" if index == 0 else "RESEARCH_WATCH"
                )
                row["trade_authorization"] = "NO_TRADE"
                row["gem_hunter_v3_trade_authorization"] = "NO_TRADE"
                row["tier_strengths_fa"] = ["قیمت، نقدینگی و حجم از DEX Screener دریافت شد."]
                row["tier_cautions_fa"] = ["تأیید مستقل منبع دوم و دادهٔ کامل هولدرها موجود نیست."]
            return rows

        engine = SimpleNamespace(data=SimpleNamespace(http=http))
        with patch.object(web_app, "ENGINE", engine), \
             patch.object(web_app, "screen_discovery_candidates", side_effect=screen), \
             patch.object(web_app, "enrich_discovery_candidates_with_technicals", side_effect=lambda rows, data, limit=1: rows), \
             patch.object(web_app, "enrich_discovery_candidates_with_pool_trades", side_effect=lambda rows, data, limit=1: rows), \
             patch.object(web_app, "evaluate_discovery_candidates", side_effect=evaluate), \
             patch.object(web_app, "process_gem_hunter_v3_candidates", side_effect=process):
            report = web_app._gem_hunter_discovery_payload(limit=10, include_diagnostics=True)

        self.assertEqual(report["qualification_engine_status"], "COMPLETE")
        self.assertEqual(report["evaluation_record_count"], 10)
        self.assertEqual(report["complete_evaluation_count"], 10)
        self.assertEqual(report["evaluation_target_count"], 10)
        self.assertEqual(report["gem_hunter_v3_tier1_count"], 0)
        self.assertEqual(report["gem_hunter_v3_tier2_count"], 1)
        self.assertEqual(report["primary_source"], "DEXSCREENER")
        self.assertEqual(report["market_reconciliation_counts"]["PASS"], 10)
        self.assertTrue(all(row["trade_authorization"] == "NO_TRADE"
                            for row in report["diagnostic_candidates"]))
        self.assertFalse(any("geckoterminal" in url for url, _ in http.calls))


if __name__ == "__main__":
    unittest.main()
