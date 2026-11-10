"""Tests for the public Gem Hunter BUY qualification gate."""
from datetime import datetime, timedelta, timezone
import unittest

from crypto_super_radar.gem_hunter_qualification import (
    FILTER_IDS,
    is_public_buy_candidate,
    public_buy_candidates,
)


NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)


def valid_candidate(now=NOW):
    data_as_of = (now - timedelta(seconds=120)).isoformat().replace('+00:00', 'Z')
    whale_time_a = (now - timedelta(seconds=600)).isoformat().replace('+00:00', 'Z')
    whale_time_b = (now - timedelta(seconds=540)).isoformat().replace('+00:00', 'Z')
    wallet_a = "11111111111111111111111111111111"
    wallet_b = "22222222222222222222222222222222"
    tx_a = "3" * 88
    tx_b = "4" * 88
    filters = {name: "PASS" for name in FILTER_IDS}
    filters["market_regime_validation"] = "UNKNOWN"
    filters["relative_strength"] = "FAIL"
    filters["momentum"] = "FAIL"
    return {
        "chain": "solana",
        "token_address": "61iusjsjCqfCW6SViHytSyYqvALUMcv3k2xMsUtM1UwP",
        "pool_address": "PoolAddress",
        "dex": "Raydium",
        "candidate_status": "BUY",
        "trade_authorization": "BUY",
        "qualification": {
            "schema_version": "gem-hunter-v1",
            "decision": "BUY",
            "market_type": "SPOT",
            "filters": filters,
            "critical_gates": {
                "security": "PASS",
                "liquidity": "PASS",
                "data_quality": "PASS",
                "exit": "PASS",
            },
            "opportunity_score": 84.0,
            "risk_score": 30.0,
            "data_as_of_utc": data_as_of,
            "market_data_sources": ["provider_a", "provider_b"],
            "market_metrics": {
                "price_usd": 0.01,
                "liquidity_usd": 185000,
                "volume_24h_usd": 420000,
                "volume_to_liquidity_24h": 2.27,
                "holder_count": 1420,
                "unique_traders_24h": 480,
                "buy_volume_share_pct": 64,
                "transactions_24h": 520,
                "pool_age_hours": 48,
                "top1_independent_holder_pct": 3.2,
                "top10_independent_holders_pct": 22,
                "related_wallet_cluster_pct": 8,
                "estimated_slippage_bps": 42,
            },
            "whale_evidence": {
                "status": "PASS",
                "coverage_status": "COMPLETE_FOR_TOKEN_LIFETIME",
                "independent_buy_wallets": 2,
                "events": [
                    {"wallet_address": wallet_a, "tx_hash": tx_a,
                     "event_type": "SWAP_BUY", "amount_usd": 28500,
                     "timestamp_utc": whale_time_a},
                    {"wallet_address": wallet_b, "tx_hash": tx_b,
                     "event_type": "SWAP_BUY", "amount_usd": 17200,
                     "timestamp_utc": whale_time_b},
                ],
            },
        },
    }


class GemHunterQualificationTests(unittest.TestCase):
    def test_complete_candidate_with_nine_passes_and_all_critical_gates_is_public(self):
        item = valid_candidate()
        self.assertTrue(is_public_buy_candidate(item, now=NOW))
        self.assertEqual(public_buy_candidates([item], now=NOW), [item])

    def test_current_unscreened_discovery_candidate_is_never_public(self):
        item = valid_candidate()
        item["candidate_status"] = "UNSCREENED"
        item["trade_authorization"] = "NO_TRADE"
        self.assertFalse(is_public_buy_candidate(item, now=NOW))
        self.assertEqual(public_buy_candidates([item], now=NOW), [])

    def test_less_than_nine_passes_is_rejected(self):
        item = valid_candidate()
        item["qualification"]["filters"]["trend"] = "FAIL"
        item["qualification"]["filters"]["momentum"] = "FAIL"
        item["qualification"]["filters"]["relative_strength"] = "FAIL"
        item["qualification"]["filters"]["market_regime_validation"] = "UNKNOWN"
        self.assertFalse(is_public_buy_candidate(item, now=NOW))

    def test_critical_gate_cannot_be_offset_by_other_passes(self):
        item = valid_candidate()
        item["qualification"]["critical_gates"]["exit"] = "FAIL"
        self.assertFalse(is_public_buy_candidate(item, now=NOW))

    def test_missing_holder_or_trader_metrics_are_rejected(self):
        item = valid_candidate()
        del item["qualification"]["market_metrics"]["holder_count"]
        self.assertFalse(is_public_buy_candidate(item, now=NOW))

    def test_stale_data_is_rejected(self):
        item = valid_candidate()
        item["qualification"]["data_as_of_utc"] = "2026-11-10T11:50:00Z"
        self.assertFalse(is_public_buy_candidate(item, now=NOW))

    def test_transfer_is_not_accepted_as_whale_swap(self):
        item = valid_candidate()
        item["qualification"]["whale_evidence"]["events"][0]["event_type"] = "TRANSFER"
        self.assertFalse(is_public_buy_candidate(item, now=NOW))

    def test_whale_evidence_requires_two_distinct_buy_wallets_and_hashes(self):
        item = valid_candidate()
        item["qualification"]["whale_evidence"]["events"][1]["wallet_address"] = item["qualification"]["whale_evidence"]["events"][0]["wallet_address"]
        self.assertFalse(is_public_buy_candidate(item, now=NOW))
        item = valid_candidate()
        item["qualification"]["whale_evidence"]["events"][0]["tx_hash"] = "not-a-hash"
        self.assertFalse(is_public_buy_candidate(item, now=NOW))

    def test_buy_score_cannot_override_risk_or_market_thresholds(self):
        item = valid_candidate()
        item["qualification"]["risk_score"] = 45
        self.assertFalse(is_public_buy_candidate(item, now=NOW))
        item = valid_candidate()
        item["qualification"]["market_metrics"]["liquidity_usd"] = 49999
        self.assertFalse(is_public_buy_candidate(item, now=NOW))

    def test_unknown_schema_or_missing_market_data_sources_fails_closed(self):
        item = valid_candidate()
        item["qualification"]["schema_version"] = "unversioned"
        self.assertFalse(is_public_buy_candidate(item, now=NOW))
        item = valid_candidate()
        item["qualification"]["market_data_sources"] = ["single_provider"]
        self.assertFalse(is_public_buy_candidate(item, now=NOW))


if __name__ == "__main__":
    unittest.main()
