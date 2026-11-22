"""Bounded, rate-limit-tolerant Gem Hunter V3 pipeline tests."""
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from crypto_super_radar import web_app


class GemHunterPipelineTests(unittest.TestCase):
    def _candidate(self, index):
        chain = ("solana", "base", "ethereum")[index % 3]
        address = f"0x{index + 1:040x}" if chain != "solana" else f"Token{index + 1:032d}"
        return {
            "chain": chain, "token_address": address,
            "pool_address": f"pool-{chain}-{index}",
            "token_name": f"Candidate {index}", "token_symbol": f"C{index}",
            "price_usd": 0.001 + index / 100000,
            "liquidity_usd": 20_000 + index * 2_000,
            "volume_24h_usd": 100_000 + index * 25_000,
            "volume_to_liquidity_24h": 5.0,
            "discovery_priority_score": index,
            "buys_24h": 70, "sells_24h": 30,
            "trade_authorization": "NO_TRADE",
        }

    def test_429_and_not_available_secondary_evidence_do_not_stall_top_ten(self):
        candidates = [self._candidate(index) for index in range(12)]
        discovery = {
            "status": "PARTIAL",
            "generated_at_utc": "2026-11-22T18:00:00Z",
            "networks": ["base", "ethereum", "solana"],
            "source_count": 2,
            "sources": {
                "GECKOTERMINAL_NEW_POOLS_SOLANA": {"status": "UNKNOWN", "reason": "HTTP 429"},
                "DEXSCREENER_LATEST_PROFILES": {"status": "PASS", "candidate_rows": 12},
            },
            "candidates": candidates,
        }

        def reconcile(rows, http, limit=10):
            self.assertEqual(len(rows), 10)
            for row in rows:
                row["market_reconciliation"] = {
                    "status": "UNKNOWN", "sources": [{"provider": "DEXSCREENER"}],
                    "source_errors": ["GeckoTerminal HTTP 429"],
                }
            return rows

        def screen(rows, data_service, limit=10):
            self.assertEqual(len(rows), 10)
            for row in rows:
                row["security_evidence"] = {
                    "status": "UNKNOWN", "mint_authority_active": False,
                    "freeze_authority_active": False, "honeypot_detected": False,
                    "sell_tax_pct": 0, "secondary_holder_data": "NOT_AVAILABLE",
                }
            return rows

        def evaluate(rows):
            now = datetime.now(timezone.utc).isoformat()
            for row in rows:
                row["qualification"] = {
                    "schema_version": web_app.SCHEMA_VERSION,
                    "decision": "NO_TRADE", "market_type": "SPOT",
                    "filters": {key: "UNKNOWN" for key in web_app.FILTER_IDS},
                    "critical_gates": {key: "UNKNOWN" for key in web_app.CRITICAL_GATES},
                    "market_metrics": {}, "data_as_of_utc": now,
                    "market_data_sources": ["DEXSCREENER"],
                    "whale_evidence": {"wallet_breadth": "NOT_AVAILABLE"},
                }
            return rows

        def process(rows, notify=True):
            for index, row in enumerate(rows):
                row["gem_hunter_v3_status"] = (
                    "TIER_1_VERIFIED_GEM" if index == 0 else
                    "TIER_2_EARLY_ALPHA" if index == 1 else "RESEARCH_WATCH"
                )
                row["tier_label_fa"] = "فرصت پژوهشی طبقه‌بندی‌شده"
                row["tier_strengths_fa"] = ["امنیت پایه و شتاب حجم مشاهده شده است."]
                row["tier_cautions_fa"] = ["داده فرعی یا تطبیق مستقل هنوز کامل نیست."]
                row["tier_base_security_status"] = "PASS"
                row["trade_authorization"] = "NO_TRADE"
                row["gem_hunter_v3_trade_authorization"] = "NO_TRADE"
            return rows

        original_cache = dict(web_app.GEM_HUNTER_CACHE)
        try:
            web_app.GEM_HUNTER_CACHE.update({"expires_at": 0.0, "payload": None})
            with patch.object(web_app, "ENGINE", SimpleNamespace(data=SimpleNamespace(http=object()))), \
                 patch.object(web_app, "discover_gem_candidates", return_value=discovery), \
                 patch.object(web_app, "reconcile_discovery_candidates", side_effect=reconcile), \
                 patch.object(web_app, "screen_discovery_candidates", side_effect=screen), \
                 patch.object(web_app, "enrich_discovery_candidates_with_technicals", side_effect=lambda rows, data, limit=1: rows), \
                 patch.object(web_app, "enrich_discovery_candidates_with_pool_trades", side_effect=lambda rows, data, limit=1: rows), \
                 patch.object(web_app, "evaluate_discovery_candidates", side_effect=evaluate), \
                 patch.object(web_app, "process_gem_hunter_v3_candidates", side_effect=process):
                report = web_app._gem_hunter_discovery_payload(limit=10, include_diagnostics=True)
        finally:
            web_app.GEM_HUNTER_CACHE.update(original_cache)

        self.assertEqual(report["evaluation_target_count"], 10)
        self.assertEqual(report["evaluation_record_count"], 10)
        self.assertEqual(report["complete_evaluation_count"], 10)
        self.assertEqual(report["qualification_engine_status"], "COMPLETE")
        self.assertEqual(report["gem_hunter_v3_tier1_count"], 1)
        self.assertEqual(report["gem_hunter_v3_tier2_count"], 1)
        self.assertEqual(report["public_policy"]["qualification_schema"], "gem-hunter-v3")
        self.assertFalse(report["public_policy"]["unknown_or_incomplete_candidates_hidden"])
        self.assertTrue(all(row["trade_authorization"] == "NO_TRADE"
                            for row in report["diagnostic_candidates"]))

    def test_shortlist_is_top_ten_by_volume_then_liquidity_across_chains(self):
        candidates = [self._candidate(index) for index in range(15)]
        selected, deferred = web_app._rank_gem_hunter_candidates(candidates)
        self.assertEqual(len(selected), 10)
        self.assertEqual(len(deferred), 5)
        self.assertEqual(selected[0]["volume_24h_usd"], max(
            row["volume_24h_usd"] for row in candidates
        ))
        self.assertEqual({row["chain"] for row in selected}, {"solana", "base", "ethereum"})


if __name__ == "__main__":
    unittest.main()
