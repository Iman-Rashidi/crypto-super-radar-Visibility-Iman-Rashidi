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
        self.assertEqual(result["filters"]["volume_confirmation"], "UNKNOWN")
        self.assertEqual(result["filters"]["liquidity_depth"], "UNKNOWN")
        self.assertEqual(result["filters"]["whale_activity"], "UNKNOWN")
        self.assertEqual(result["filters"]["holder_distribution"], "UNKNOWN")
        self.assertEqual(result["critical_gates"]["security"], "UNKNOWN")
        self.assertEqual(result["critical_gates"]["liquidity"], "UNKNOWN")
        self.assertEqual(result["critical_gates"]["data_quality"], "UNKNOWN")
        self.assertEqual(result["critical_gates"]["exit"], "UNKNOWN")
        self.assertEqual(set(result["critical_gate_reasons"]), set(CRITICAL_GATES))
        self.assertIn("عمق اجرایی", result["critical_gate_reasons"]["exit"])
        self.assertIsNone(result["market_metrics"]["holder_count"])
        self.assertIsNone(result["market_metrics"]["buy_volume_share_pct"])
        self.assertIsNone(result["market_metrics"]["estimated_slippage_bps"])
        self.assertEqual(result["whale_evidence"]["events"], [])
        self.assertEqual(result["decision"], "NO_TRADE")
        self.assertEqual(result["evaluation_status"], "INCOMPLETE")
        self.assertIsNone(result["market_metrics"]["price_usd"])
        self.assertAlmostEqual(result["market_metrics"]["observed_price_usd"], 0.01)
        self.assertEqual(result["market_metrics"]["price_verification_status"], "UNKNOWN")
        self.assertEqual(result["market_data_sources"], [])

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

    def test_only_reconciled_exact_pool_can_pass_market_gates(self):
        item = candidate()
        item["market_reconciliation"] = {
            "status": "PASS",
            "verified_liquidity_usd": 180000,
            "verified_volume_24h_usd": 360000,
            "verified_price_usd": 0.01,
            "sources": [{"provider": "DEXSCREENER"}, {"provider": "GECKOTERMINAL"}],
        }
        result = evaluate_discovery_candidate(item, now=NOW)
        self.assertEqual(result["critical_gates"]["liquidity"], "PASS")
        self.assertEqual(result["critical_gates"]["data_quality"], "PASS")
        self.assertEqual(result["filters"]["volume_confirmation"], "PASS")
        self.assertEqual(result["filters"]["liquidity_depth"], "UNKNOWN")
        self.assertEqual(set(result["market_data_sources"]), {"DEXSCREENER", "GECKOTERMINAL"})

    def test_verified_liquidity_controls_floor_when_reported_value_is_just_below_floor(self):
        item = candidate()
        item["liquidity_usd"] = 49_999
        item["market_reconciliation"] = {
            "status": "PASS",
            "verified_price_usd": 0.01,
            "verified_liquidity_usd": 50_001,
            "verified_volume_24h_usd": 360_000,
            "sources": [{"provider": "DEXSCREENER"}, {"provider": "GECKOTERMINAL"}],
        }
        result = evaluate_discovery_candidate(item, now=NOW)
        self.assertEqual(result["critical_gates"]["liquidity"], "PASS")
        self.assertEqual(result["market_metrics"]["liquidity_usd"], 50_001)
        self.assertEqual(result["market_metrics"]["observed_liquidity_usd"], 49_999)

    def test_market_data_disagreement_fails_liquidity_and_data_quality(self):
        item = candidate()
        item["market_reconciliation"] = {
            "status": "FAIL",
            "verified_liquidity_usd": 0.0004,
            "verified_volume_24h_usd": 13761,
            "sources": [{"provider": "DEXSCREENER"}, {"provider": "GECKOTERMINAL"}],
        }
        result = evaluate_discovery_candidate(item, now=NOW)
        self.assertEqual(result["critical_gates"]["liquidity"], "FAIL")
        self.assertEqual(result["critical_gates"]["data_quality"], "FAIL")
        self.assertEqual(result["filters"]["volume_confirmation"], "FAIL")
        self.assertEqual(result["decision"], "NO_TRADE")

    def test_limited_provider_pass_is_not_full_token_security_pass(self):
        item = candidate()
        item["security_evidence"] = {
            "status": "PASS", "provider": "GOPLUS_SOURCIFY",
            "checked_at_utc": "2026-11-11T11:59:00Z",
            "source_verified": True, "flags": {"is_honeypot": False},
        }
        result = evaluate_discovery_candidate(item, now=NOW)
        self.assertEqual(result["filters"]["contract_security"], "UNKNOWN")
        self.assertEqual(result["critical_gates"]["security"], "UNKNOWN")
        self.assertEqual(result["token_risk_assessment"]["incomplete_check_count"], 5)

    def test_source_backed_security_failure_is_preserved(self):
        item = candidate()
        item["security_evidence"] = {
            "status": "FAIL", "provider": "GOPLUS_SOURCIFY",
            "checked_at_utc": "2026-11-11T11:59:00Z",
            "flags": {"is_honeypot": True},
        }
        result = evaluate_discovery_candidate(item, now=NOW)
        self.assertEqual(result["filters"]["contract_security"], "FAIL")
        self.assertEqual(result["critical_gates"]["security"], "FAIL")

    def test_all_five_complete_token_risk_checks_can_pass_security_subgate(self):
        item = candidate()
        item["token_risk_evidence"] = {
            name: {
                "status": "PASS", "source": "verified-provider-" + name,
                "checked_at": "2026-11-11T11:59:00Z", "coverage_pct": 100,
                "value": {"observed": True},
            }
            for name in (
                "contract_authority", "holder_concentration", "deployer_history",
                "launch_bundle", "liquidity_exit",
            )
        }
        result = evaluate_discovery_candidate(item, now=NOW)
        self.assertEqual(result["critical_gates"]["security"], "PASS")
        self.assertEqual(result["token_risk_assessment"]["passed_check_count"], 5)
        self.assertEqual(result["token_risk_assessment"]["trade_authorization"], "NO_TRADE")

    def test_solana_mint_or_freeze_authority_failure_is_preserved(self):
        item = candidate()
        item["security_evidence"] = {
            "status": "FAIL", "provider": "SOLANA_RPC",
            "checked_at_utc": "2026-11-11T11:59:00Z",
            "mint_authority_active": True,
            "freeze_authority_active": False,
            "reason_fa": ["اختیار ضرب فعال است."],
        }
        result = evaluate_discovery_candidate(item, now=NOW)
        self.assertEqual(result["critical_gates"]["security"], "FAIL")
        self.assertEqual(
            result["token_risk_assessment"]["failed_checks"],
            ["contract_authority"],
        )

    def test_missing_timestamp_does_not_support_source_backed_fail(self):
        item = candidate()
        item["security_evidence"] = {
            "status": "FAIL", "provider": "GOPLUS_SOURCIFY",
            "flags": {"is_honeypot": True},
        }
        result = evaluate_discovery_candidate(item, now=NOW)
        self.assertEqual(result["critical_gates"]["security"], "UNKNOWN")

    def test_batch_evaluator_keeps_discovery_untradeable_and_preserves_existing_records(self):
        raw = candidate()
        evaluated = evaluate_discovery_candidates([raw], now=NOW)
        self.assertEqual(evaluated[0]["candidate_status"], "UNSCREENED")
        self.assertEqual(evaluated[0]["trade_authorization"], "NO_TRADE")
        self.assertIn("qualification", evaluated[0])
        existing = dict(raw, qualification={"schema_version": "existing"})
        preserved = evaluate_discovery_candidates([existing], now=NOW)
        self.assertEqual(preserved[0]["qualification"], {"schema_version": "existing"})


    def test_fresh_complete_exact_pool_technical_evidence_populates_four_filters(self):
        item = candidate()
        item["technical_evidence"] = {
            "schema_version": "gem-hunter-technical-v1",
            "status": "COMPLETE",
            "coverage_status": "COMPLETE",
            "chain": item["chain"],
            "token_address": item["token_address"],
            "pool_address": item["pool_address"],
            "checked_at_utc": "2026-11-11T11:59:00Z",
            "sources": ["GECKOTERMINAL", "BINANCE_SPOT"],
            "filters": {
                "trend": "PASS",
                "momentum": "PASS",
                "relative_strength": "FAIL",
                "market_regime_validation": "UNKNOWN",
            },
            "filter_coverage": {
                "trend": True, "momentum": True,
                "relative_strength": True, "market_regime_validation": True,
            },
            "metrics": {"token_return_7d_pct": 12.0},
            "timeframes": {"4h": {"bars": 120}, "1d": {"bars": 90}},
        }
        result = evaluate_discovery_candidate(item, now=NOW)
        self.assertEqual(result["filters"]["trend"], "PASS")
        self.assertEqual(result["filters"]["momentum"], "PASS")
        self.assertEqual(result["filters"]["relative_strength"], "FAIL")
        self.assertEqual(result["filters"]["market_regime_validation"], "UNKNOWN")
        self.assertEqual(result["market_metrics"]["technical_evidence_status"], "COMPLETE")
        self.assertEqual(result["market_metrics"]["technical_metrics"]["token_return_7d_pct"], 12.0)

    def test_stale_or_wrong_pool_technical_evidence_cannot_upgrade_filters(self):
        item = candidate()
        evidence = {
            "schema_version": "gem-hunter-technical-v1",
            "status": "COMPLETE",
            "coverage_status": "COMPLETE",
            "chain": item["chain"],
            "token_address": item["token_address"],
            "pool_address": "DifferentPool",
            "checked_at_utc": "2026-11-11T11:59:00Z",
            "sources": ["GECKOTERMINAL", "BINANCE_SPOT"],
            "filters": {
                "trend": "PASS", "momentum": "PASS",
                "relative_strength": "PASS", "market_regime_validation": "PASS",
            },
        }
        item["technical_evidence"] = evidence
        result = evaluate_discovery_candidate(item, now=NOW)
        self.assertEqual(result["filters"]["trend"], "UNKNOWN")
        evidence["pool_address"] = item["pool_address"]
        evidence["checked_at_utc"] = "2026-11-11T10:00:00Z"
        result = evaluate_discovery_candidate(item, now=NOW)
        self.assertEqual(result["filters"]["trend"], "UNKNOWN")



    def test_partial_technical_evidence_only_upgrades_covered_filters(self):
        item = candidate()
        item["technical_evidence"] = {
            "schema_version": "gem-hunter-technical-v1",
            "status": "PARTIAL",
            "coverage_status": "PARTIAL",
            "chain": item["chain"],
            "token_address": item["token_address"],
            "pool_address": item["pool_address"],
            "checked_at_utc": "2026-11-11T11:59:00Z",
            "sources": ["GECKOTERMINAL"],
            "filters": {
                "trend": "PASS", "momentum": "PASS",
                "relative_strength": "UNKNOWN",
                "market_regime_validation": "UNKNOWN",
            },
            "filter_coverage": {
                "trend": True, "momentum": True,
                "relative_strength": False, "market_regime_validation": False,
            },
            "metrics": {}, "timeframes": {},
        }
        result = evaluate_discovery_candidate(item, now=NOW)
        self.assertEqual(result["filters"]["trend"], "PASS")
        self.assertEqual(result["filters"]["momentum"], "PASS")
        self.assertEqual(result["filters"]["relative_strength"], "UNKNOWN")
        self.assertEqual(result["filters"]["market_regime_validation"], "UNKNOWN")
        self.assertEqual(result["market_metrics"]["technical_evidence_status"], "PARTIAL")



    def test_complete_exact_pool_trade_evidence_computes_buy_sell_pressure(self):
        item = candidate()
        item["pool_trade_evidence"] = {
            "schema_version": "gem-hunter-pool-trades-v1",
            "status": "COMPLETE",
            "coverage_status": "COMPLETE",
            "chain": item["chain"],
            "token_address": item["token_address"],
            "pool_address": item["pool_address"],
            "checked_at_utc": "2026-11-11T11:59:00Z",
            "source": "GECKOTERMINAL",
            "buy_sell_pressure_status": "PASS",
            "trade_count": 40,
            "unique_trader_count": 35,
            "buy_volume_usd": 80000.0,
            "sell_volume_usd": 20000.0,
            "total_volume_usd": 100000.0,
            "net_swap_flow_usd": 60000.0,
            "whale_buy_count": 2,
            "whale_sell_count": 1,
            "whale_events": [{"event_type": "SWAP_BUY", "amount_usd": 5000.0}],
            "wash_trading_signal": "NO_STRONG_PATTERN_IN_SAMPLE",
        }
        result = evaluate_discovery_candidate(item, now=NOW)
        self.assertEqual(result["filters"]["buy_sell_pressure"], "PASS")
        self.assertEqual(result["market_metrics"]["pool_trade_evidence_status"], "COMPLETE")
        self.assertEqual(result["market_metrics"]["pool_net_swap_flow_usd"], 60000.0)
        self.assertEqual(result["filters"]["capital_flow"], "UNKNOWN")
        self.assertEqual(result["filters"]["whale_activity"], "UNKNOWN")
        self.assertEqual(result["whale_evidence"]["coverage_status"], "PARTIAL_24H_SAMPLE")
        self.assertEqual(result["whale_evidence"]["events"][0]["event_type"], "SWAP_BUY")

    def test_partial_stale_or_wrong_pool_trade_evidence_cannot_upgrade_pressure(self):
        item = candidate()
        evidence = {
            "schema_version": "gem-hunter-pool-trades-v1",
            "status": "PARTIAL",
            "coverage_status": "PARTIAL",
            "chain": item["chain"],
            "token_address": item["token_address"],
            "pool_address": "DifferentPool",
            "checked_at_utc": "2026-11-11T11:59:00Z",
            "source": "GECKOTERMINAL",
            "buy_sell_pressure_status": "PASS",
            "buy_volume_usd": 90000.0,
            "sell_volume_usd": 10000.0,
            "total_volume_usd": 100000.0,
            "net_swap_flow_usd": 80000.0,
        }
        item["pool_trade_evidence"] = evidence
        result = evaluate_discovery_candidate(item, now=NOW)
        self.assertEqual(result["filters"]["buy_sell_pressure"], "UNKNOWN")
        evidence["pool_address"] = item["pool_address"]
        evidence["status"] = "COMPLETE"
        evidence["coverage_status"] = "COMPLETE"
        evidence["checked_at_utc"] = "2026-11-11T10:00:00Z"
        result = evaluate_discovery_candidate(item, now=NOW)
        self.assertEqual(result["filters"]["buy_sell_pressure"], "UNKNOWN")

    def test_suspected_wash_pattern_is_warning_not_automated_proof(self):
        item = candidate()
        item["pool_trade_evidence"] = {
            "schema_version": "gem-hunter-pool-trades-v1",
            "status": "COMPLETE",
            "coverage_status": "COMPLETE",
            "chain": item["chain"],
            "token_address": item["token_address"],
            "pool_address": item["pool_address"],
            "checked_at_utc": "2026-11-11T11:59:00Z",
            "source": "GECKOTERMINAL",
            "buy_sell_pressure_status": "UNKNOWN",
            "buy_volume_usd": 5000.0,
            "sell_volume_usd": 5000.0,
            "total_volume_usd": 10000.0,
            "net_swap_flow_usd": 0.0,
            "wash_trading_signal": "SUSPECTED",
            "whale_events": [],
        }
        result = evaluate_discovery_candidate(item, now=NOW)
        self.assertEqual(result["filters"]["market_integrity"], "UNKNOWN")
        self.assertIn("مشکوک", result["filter_reasons"]["market_integrity"])
        self.assertEqual(result["filters"]["buy_sell_pressure"], "UNKNOWN")



if __name__ == "__main__":
    unittest.main()
