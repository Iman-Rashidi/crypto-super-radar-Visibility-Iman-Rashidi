"""Tests for evidence-bounded Gem Hunter first-stage evaluation."""
from datetime import datetime, timezone
import unittest

from crypto_super_radar.gem_hunter_evaluator import (
    CRITICAL_GATES, FILTER_IDS, evaluate_discovery_candidate,
    evaluate_discovery_candidates,
)

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)


def candidate():
    return {
        "chain": "solana",
        "token_address": "61iusjsjCqfCW6SViHytSyYqvALUMcv3k2xMsUtM1UwP",
        "pool_address": "PoolAddress",
        "dex": "Raydium",
        "price_usd": 0.01,
        "liquidity_usd": 180000,
        "volume_24h_usd": 360000,
        "volume_to_liquidity_24h": 2.0,
        "pool_age_hours": 48,
        "buys_24h": 250,
        "sells_24h": 100,
        "price_change_1h_pct": 4.0,
        "sources": ["GECKOTERMINAL_NEW_POOLS_SOLANA", "GECKOTERMINAL_TRENDING_POOLS_SOLANA"],
    }


class GemHunterEvaluatorTests(unittest.TestCase):
    def test_emits_all_filters_and_gates_without_inventing_missing_evidence(self):
        result = evaluate_discovery_candidate(candidate(), now=NOW)
        self.assertEqual(set(result["filters"]), set(FILTER_IDS))
        self.assertEqual(set(result["critical_gates"]), set(CRITICAL_GATES))
        self.assertEqual(result["filters"]["volume_confirmation"], "PASS")
        self.assertEqual(result["filters"]["liquidity_depth"], "UNKNOWN")
        self.assertEqual(result["filters"]["whale_activity"], "UNKNOWN")
        self.assertEqual(result["filters"]["holder_distribution"], "UNKNOWN")
        self.assertEqual(result["critical_gates"]["security"], "UNKNOWN")
        self.assertEqual(result["critical_gates"]["data_quality"], "UNKNOWN")
        self.assertEqual(result["critical_gates"]["exit"], "UNKNOWN")
        self.assertIsNone(result["market_metrics"]["holder_count"])
        self.assertIsNone(result["market_metrics"]["buy_volume_share_pct"])
        self.assertIsNone(result["market_metrics"]["estimated_slippage_bps"])
        self.assertEqual(result["whale_evidence"]["events"], [])
        self.assertEqual(result["decision"], "NO_TRADE")
        self.assertEqual(result["evaluation_status"], "INCOMPLETE")

    def test_transaction_counts_are_not_misrepresented_as_notional_buy_share(self):
        result = evaluate_discovery_candidate(candidate(), now=NOW)
        self.assertIsNone(result["market_metrics"]["buy_volume_share_pct"])
        self.assertEqual(result["filters"]["buy_sell_pressure"], "UNKNOWN")

    def test_low_liquidity_fails_market_gate(self):
        item = candidate()
        item["liquidity_usd"] = 10000
        item["volume_to_liquidity_24h"] = 2.0
        result = evaluate_discovery_candidate(item, now=NOW)
        self.assertEqual(result["critical_gates"]["liquidity"], "FAIL")
        self.assertEqual(result["filters"]["liquidity_depth"], "FAIL")

    def test_batch_evaluator_keeps_discovery_untradeable_and_preserves_existing_records(self):
        raw = candidate()
        evaluated = evaluate_discovery_candidates([raw], now=NOW)
        self.assertEqual(evaluated[0]["candidate_status"], "UNSCREENED")
        self.assertEqual(evaluated[0]["trade_authorization"], "NO_TRADE")
        self.assertIn("qualification", evaluated[0])
        existing = dict(raw, qualification={"schema_version": "existing"})
        preserved = evaluate_discovery_candidates([existing], now=NOW)
        self.assertEqual(preserved[0]["qualification"], {"schema_version": "existing"})


if __name__ == "__main__":
    unittest.main()
