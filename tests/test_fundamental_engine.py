import unittest

from crypto_super_radar.fundamental_engine import (
    MODULE_WEIGHTS,
    build_fundamental_report,
    classify_industry,
    comparable_projects,
    discounted_protocol_cashflows,
    industry_analysis,
    accounting_principles_analysis,
    market_analysis,
    dividend_analysis,
    mergers_acquisitions_analysis,
    sector_rotation_analysis,
    share_buyback_analysis,
    eps_analysis,
    esg_analysis,
)


class IndustryAnalysisTests(unittest.TestCase):
    def test_sector_classification_requires_sector(self):
        self.assertEqual(classify_industry({})["status"], "UNKNOWN")
        self.assertEqual(classify_industry({"sector": "DeFi"})["sector"], "defi")

    def test_industry_score_tracks_known_criteria_and_missingness(self):
        evidence = {
            "market_growth": {"status": "KNOWN", "score": 80},
            "competition": {"status": "KNOWN", "score": 60},
        }
        result = industry_analysis(evidence)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["score"], 70)
        self.assertIn("regulatory_environment", result["unknown_criteria"])

    def test_invalid_industry_score_is_not_counted(self):
        result = industry_analysis({"market_growth": {"status": "KNOWN", "score": 101}})
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertIsNone(result["score"])


class ValuationTests(unittest.TestCase):
    def test_dcf_requires_explicit_token_value_capture(self):
        result = discounted_protocol_cashflows(
            [100, 120], discount_rate=0.2, terminal_growth_rate=0.03,
            net_treasury_value=10,
        )
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertIsNone(result["estimated_value"])

    def test_dcf_calculates_captured_cashflows_and_terminal_value(self):
        result = discounted_protocol_cashflows(
            [100, 120], discount_rate=0.2, terminal_growth_rate=0.03,
            token_value_capture_ratio=0.5, net_treasury_value=10,
        )
        self.assertEqual(result["status"], "KNOWN")
        self.assertGreater(result["estimated_value"], 0)
        self.assertEqual(result["token_value_capture_ratio"], 0.5)

    def test_dcf_rejects_invalid_rates_and_negative_cashflows(self):
        invalid_rates = discounted_protocol_cashflows(
            [100], discount_rate=0.1, terminal_growth_rate=0.1,
            token_value_capture_ratio=0.5,
        )
        negative = discounted_protocol_cashflows(
            [-10, 100], discount_rate=0.2, terminal_growth_rate=0.02,
            token_value_capture_ratio=0.5,
        )
        self.assertEqual(invalid_rates["status"], "UNKNOWN")
        self.assertEqual(negative["status"], "UNKNOWN")

    def test_comparables_only_use_same_sector_and_positive_denominators(self):
        project = {"sector": "defi", "market_cap": 1000, "revenue": 100,
                   "fdv": 2000, "tvl": 500}
        peers = [
            {"sector": "defi", "market_cap": 500, "revenue": 100,
             "fdv": 1000, "tvl": 500},
            {"sector": "gaming", "market_cap": 100, "revenue": 10,
             "fdv": 200, "tvl": 50},
            {"sector": "defi", "market_cap": 300, "revenue": 0,
             "fdv": 600, "tvl": 200},
        ]
        result = comparable_projects(project, peers)
        self.assertEqual(result["status"], "KNOWN")
        self.assertEqual(result["peer_count"], 2)
        self.assertEqual(result["excluded_non_comparable_peers"], 1)
        self.assertEqual(result["peer_medians"]["market_cap_to_revenue"], 5.0)
        self.assertEqual(result["relative_valuation"]["market_cap_to_revenue"]["relative_to_peers_pct"], 100.0)


class AccountingAndMarketAnalysisTests(unittest.TestCase):
    def test_inventory_and_depreciation_can_be_not_applicable_without_positive_score(self):
        result = accounting_principles_analysis({
            "inventory_valuation": {"applicability": "NOT_APPLICABLE"},
            "depreciation": {"applicability": "NOT_APPLICABLE"},
        })
        self.assertEqual(result["principles"]["inventory_valuation"]["status"], "NOT_APPLICABLE")
        self.assertEqual(result["principles"]["depreciation"]["status"], "NOT_APPLICABLE")
        self.assertIsNone(result["principles"]["inventory_valuation"]["score"])

    def test_accounting_methods_are_only_accepted_from_known_disclosures(self):
        result = accounting_principles_analysis({
            "inventory_valuation": {
                "applicability": "APPLICABLE",
                "valuation_method": "FIFO",
                "inventory_scope_disclosed": "PASS",
                "valuation_method_disclosed": "PASS",
                "impairment_or_obsolescence_disclosed": "PASS",
            },
            "depreciation": {
                "applicability": "APPLICABLE",
                "depreciation_method": "STRAIGHT_LINE",
            },
        })
        self.assertEqual(result["principles"]["inventory_valuation"]["reported_method"], "FIFO")
        self.assertEqual(result["principles"]["depreciation"]["reported_method"], "STRAIGHT_LINE")
        self.assertIn("WEIGHTED_AVERAGE", result["principles"]["inventory_valuation"]["accepted_methods"])

    def test_missing_accounting_evidence_remains_unknown(self):
        result = accounting_principles_analysis({})
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["principles"]["revenue_recognition"]["status"], "UNKNOWN")

    def test_revenue_recognition_failure_is_flagged(self):
        result = accounting_principles_analysis({
            "revenue_recognition": {
                "performance_obligation_satisfied": "PASS",
                "revenue_basis_disclosed": "FAIL",
                "fees_separated_from_tokenholder_revenue": "UNKNOWN",
                "collectability_supported": "PASS",
            }
        })
        self.assertEqual(result["principles"]["revenue_recognition"]["status"], "FAIL")

    def test_market_analysis_requires_timestamped_source(self):
        result = market_analysis({
            "gdp_growth": {"status": "KNOWN", "value": 2.1, "source": "official"},
            "inflation": {"status": "KNOWN", "value": 2.5, "source": "official",
                          "observed_at_utc": "2026-11-01T00:00:00+00:00"},
        })
        self.assertEqual(result["observations"]["gdp_growth"]["status"], "UNKNOWN")
        self.assertEqual(result["observations"]["inflation"]["status"], "KNOWN")
        self.assertEqual(result["status"], "PARTIAL")

    def test_supporting_modules_do_not_change_seven_module_score_weights(self):
        result = build_fundamental_report({
            "accounting_principles": {
                "inventory_valuation": {"applicability": "NOT_APPLICABLE"},
            },
            "market_analysis": {},
        })
        self.assertEqual(set(result["weights"]), {
            "macro", "tokenomics", "onchain", "project", "industry", "dcf", "comparables"
        })
        self.assertIn("accounting_principles", result["modules"])
        self.assertIn("market_analysis", result["modules"])
        self.assertEqual(result["modules"]["market_analysis"]["status"], "UNKNOWN")
        self.assertIsNone(result["score"])


class FundamentalReportTests(unittest.TestCase):
    def test_missing_modules_never_become_zero_and_block_score(self):
        result = build_fundamental_report({})
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertIsNone(result["score"])
        self.assertEqual(result["modules"]["macro"]["status"], "UNKNOWN")
        self.assertFalse(result["trade_authorized"])

    def test_coverage_threshold_is_enforced(self):
        result = build_fundamental_report({
            "macro": {"status": "KNOWN", "score": 80},
            "tokenomics": {"status": "KNOWN", "score": 70},
            "onchain": {"status": "KNOWN", "score": 60},
        })
        self.assertEqual(result["status"], "INSUFFICIENT_COVERAGE")
        self.assertIsNone(result["score"])

    def test_full_coverage_uses_declared_weights(self):
        evidence = {
            "macro": {"status": "KNOWN", "score": 80},
            "tokenomics": {"status": "KNOWN", "score": 70},
            "onchain": {"status": "KNOWN", "score": 60},
            "project": {"status": "KNOWN", "score": 90},
            "industry": {key: {"status": "KNOWN", "score": 75} for key in (
                "market_growth", "market_share", "competition", "entry_barriers",
                "technology_trend", "regulatory_environment", "adoption")},
            "dcf": {"status": "KNOWN", "score": 65},
            "comparables": {"status": "KNOWN", "score": 85},
        }
        result = build_fundamental_report(evidence)
        expected = sum(MODULE_WEIGHTS[key] * result["modules"][key]["score"]
                       for key in MODULE_WEIGHTS) / 100
        self.assertEqual(result["status"], "KNOWN")
        self.assertEqual(result["coverage_pct"], 100.0)
        self.assertEqual(result["score"], round(expected, 2))
        self.assertFalse(result["trade_authorized"])

class DividendAnalysisTests(unittest.TestCase):
    def test_crypto_is_not_applicable_not_a_negative_score(self):
        result = dividend_analysis({"asset_class": "crypto"})
        self.assertEqual(result["status"], "NOT_APPLICABLE")
        self.assertIsNone(result["score"])

    def test_equity_dividend_metrics_and_growth(self):
        result = dividend_analysis({"asset_class": "equity", "current_price": 100,
            "annual_dividend_per_share": 5, "net_income": 10_000_000,
            "dividends_paid": 4_000_000, "free_cash_flow": 6_000_000,
            "dividend_history": [{"year": 2022, "dividend_per_share": 4.0},
                                 {"year": 2023, "dividend_per_share": 4.2},
                                 {"year": 2024, "dividend_per_share": 4.5}],
            "price_change_12m_pct": 2, "source": "https://example.org/filing",
            "observed_at_utc": "2026-11-01T00:00:00Z"})
        self.assertEqual(result["status"], "KNOWN")
        self.assertAlmostEqual(result["dividend_yield_pct"], 5.0)
        self.assertAlmostEqual(result["payout_ratio_pct"], 40.0)
        self.assertAlmostEqual(result["dividend_coverage_ratio"], 2.5)
        self.assertTrue(result["history_consistent"])
        self.assertGreater(result["dividend_growth_cagr_pct"], 0)
        self.assertEqual(result["dividend_sustainability"], "HEALTHY")

    def test_high_yield_after_price_crash_flags_dividend_trap(self):
        result = dividend_analysis({"asset_class": "stock", "dividend_yield_pct": 10,
            "price_change_12m_pct": -50, "source": "https://example.org",
            "observed_at_utc": "2026-11-01T00:00:00Z"})
        self.assertTrue(result["dividend_trap_warning"])

    def test_missing_source_does_not_validate_metrics(self):
        result = dividend_analysis({"asset_class": "equity", "current_price": 100,
            "annual_dividend_per_share": 5, "net_income": 100, "dividends_paid": 20})
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["checks"]["dividend_yield"], "UNKNOWN")

    def test_dividend_diagnostic_does_not_change_seven_module_weights(self):
        result = build_fundamental_report({"dividend_analysis": {"asset_class": "crypto"}})
        self.assertEqual(set(result["weights"]), set(MODULE_WEIGHTS))
        self.assertEqual(result["modules"]["dividend_analysis"]["status"], "NOT_APPLICABLE")


class ESGAnalysisTests(unittest.TestCase):
    def _pillar(self, score, source):
        return {"status": "KNOWN", "score": score, "source_url": source,
                "observed_at_utc": "2026-11-14T10:00:00Z",
                "metrics": {"sample_metric": 1}}

    def test_company_three_pillars_are_scored_separately(self):
        result = esg_analysis({
            "asset_class": "equity",
            "environmental": self._pillar(80, "https://example.org/environment"),
            "social": self._pillar(70, "https://example.org/social"),
            "governance": self._pillar(90, "https://example.org/governance"),
        })
        self.assertEqual(result["status"], "KNOWN")
        self.assertEqual(result["score"], 80)
        self.assertEqual(result["known_pillars"], 3)
        self.assertEqual(result["asset_type"], "company")

    def test_crypto_network_uses_same_pillars_but_crypto_context(self):
        result = esg_analysis({
            "asset_class": "crypto",
            "environmental": self._pillar(40, "https://example.org/energy"),
            "social": self._pillar(75, "https://example.org/community"),
            "governance": self._pillar(55, "https://example.org/validators"),
        })
        self.assertEqual(result["status"], "KNOWN")
        self.assertEqual(result["asset_type"], "crypto_network_or_token")
        self.assertEqual(result["score"], round((40 + 75 + 55) / 3, 2))
        self.assertIn("تمرکز اعتبارسنج", result["pillars"]["governance"]["description_fa"])

    def test_missing_pillar_is_partial_not_zero(self):
        result = esg_analysis({
            "asset_class": "stock",
            "environmental": self._pillar(80, "https://example.org/environment"),
        })
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["score"], 80)
        self.assertEqual(result["pillars"]["social"]["status"], "UNKNOWN")
        self.assertIsNone(result["pillars"]["social"]["score"])

    def test_pillar_without_source_or_timestamp_is_unknown(self):
        result = esg_analysis({
            "asset_class": "equity",
            "environmental": {"status": "KNOWN", "score": 100},
        })
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertIsNone(result["score"])

    def test_invalid_scores_and_unverified_controversies_are_ignored(self):
        result = esg_analysis({
            "asset_class": "equity",
            "environmental": self._pillar(120, "https://example.org/environment"),
            "controversies": [{"description": "unverified claim"}],
        })
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["controversy_count"], 0)

    def test_sourced_controversy_is_retained_as_context_not_score(self):
        result = esg_analysis({
            "asset_class": "equity",
            "controversies": [{
                "description": "Regulatory investigation disclosed",
                "severity": "HIGH", "pillar": "governance",
                "source_url": "https://example.org/investigation",
                "observed_at_utc": "2026-11-14T10:00:00Z",
            }],
        })
        self.assertEqual(result["controversy_count"], 1)
        self.assertEqual(result["controversies"][0]["severity"], "HIGH")
        self.assertIsNone(result["score"])

    def test_unknown_asset_class_is_not_applicable_or_negative(self):
        result = esg_analysis({})
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["applicability"], "UNKNOWN")
        self.assertIsNone(result["score"])

    def test_esg_does_not_change_weighted_modules_or_trade_authorization(self):
        report = build_fundamental_report({
            "esg_analysis": {"asset_class": "crypto",
                "environmental": self._pillar(50, "https://example.org/energy")}
        })
        self.assertEqual(set(report["weights"]), set(MODULE_WEIGHTS))
        self.assertEqual(report["modules"]["esg_analysis"]["status"], "PARTIAL")
        self.assertFalse(report["trade_authorized"])



class ShareBuybackAnalysisTests(unittest.TestCase):
    def test_crypto_is_not_applicable(self):
        result = share_buyback_analysis({"asset_class": "crypto"})
        self.assertEqual(result["status"], "NOT_APPLICABLE")
        self.assertIsNone(result["score"])

    def test_buyback_yield_funding_and_mechanical_eps(self):
        result = share_buyback_analysis({
            "asset_class": "equity", "completed_shares_repur": 100_000,
            "shares_outstanding_before": 1_000_000,
            "shares_outstanding_after": 900_000,
            "average_repurchase_price": 10, "current_price": 9,
            "cash_spent": 1_000_000, "debt_funded_amount": 500_000,
            "net_income": 1_000_000, "source": "https://example.org/filing",
            "observed_at_utc": "2026-11-01T00:00:00Z",
        })
        self.assertEqual(result["status"], "KNOWN")
        self.assertAlmostEqual(result["buyback_yield_pct"], 10)
        self.assertAlmostEqual(result["share_reduction_pct"], 10)
        self.assertAlmostEqual(result["mechanical_eps_before"], 1)
        self.assertAlmostEqual(result["mechanical_eps_after"], 1_000_000 / 900_000)
        self.assertAlmostEqual(result["mechanical_eps_accretion_pct"], 11.111111, places=4)
        self.assertAlmostEqual(result["debt_funding_pct"], 50)
        self.assertIn("debt_funded_repurchase", result["review_flags"])
        self.assertIn("repurchased_above_current_price_reference", result["review_flags"])

    def test_authorized_but_unexecuted_program_is_not_completed(self):
        result = share_buyback_analysis({
            "asset_class": "equity", "authorized_amount": 5_000_000,
            "source": "https://example.org/filing",
            "observed_at_utc": "2026-11-01T00:00:00Z",
        })
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertIsNone(result["completed_shares_repur"])

    def test_missing_source_keeps_buyback_unknown(self):
        result = share_buyback_analysis({"asset_class": "equity",
            "completed_shares_repur": 100, "shares_outstanding_before": 1000,
            "shares_outstanding_after": 900})
        self.assertEqual(result["status"], "UNKNOWN")


class EPSAnalysisTests(unittest.TestCase):
    def test_crypto_is_not_applicable(self):
        result = eps_analysis({"asset_class": "token"})
        self.assertEqual(result["status"], "NOT_APPLICABLE")
        self.assertIsNone(result["score"])

    def test_basic_diluted_adjusted_eps_and_pe(self):
        result = eps_analysis({
            "asset_class": "stock", "net_income": 1_000_000,
            "preferred_dividends": 100_000,
            "weighted_average_shares": 900_000,
            "diluted_weighted_average_shares": 1_000_000,
            "adjusted_net_income": 990_000, "current_price": 20,
            "eps_history": [{"year": 2023, "eps": 0.8},
                            {"year": 2024, "eps": 0.9},
                            {"year": 2025, "eps": 1.0}],
            "forecast_eps": 1.2, "source": "https://example.org/annual-report",
            "observed_at_utc": "2026-11-01T00:00:00Z",
        })
        self.assertEqual(result["status"], "KNOWN")
        self.assertAlmostEqual(result["basic_eps"], 1.0)
        self.assertAlmostEqual(result["diluted_eps"], 0.9)
        self.assertAlmostEqual(result["adjusted_eps"], 1.1)
        self.assertAlmostEqual(result["pe_ratio"], 20)
        self.assertAlmostEqual(result["forecast_change_pct"], 20)
        self.assertGreater(result["eps_history_cagr_pct"], 0)

    def test_negative_eps_does_not_produce_pe(self):
        result = eps_analysis({"asset_class": "equity", "net_income": -100,
            "weighted_average_shares": 10, "current_price": 5,
            "source": "https://example.org/filing",
            "observed_at_utc": "2026-11-01T00:00:00Z"})
        self.assertIsNone(result["pe_ratio"])
        self.assertEqual(result["status"], "PARTIAL")

    def test_diagnostics_do_not_change_weighted_modules(self):
        result = build_fundamental_report({
            "share_buybacks": {"asset_class": "crypto"},
            "eps_analysis": {"asset_class": "crypto"},
        })
        self.assertEqual(set(result["weights"]), set(MODULE_WEIGHTS))
        self.assertEqual(result["modules"]["share_buybacks"]["status"], "NOT_APPLICABLE")
        self.assertEqual(result["modules"]["eps_analysis"]["status"], "NOT_APPLICABLE")
        self.assertFalse(result["trade_authorized"])



class MergersAcquisitionsTests(unittest.TestCase):
    def test_missing_deal_evidence_is_unknown(self):
        result = mergers_acquisitions_analysis({"asset_class": "equity"})
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["deal_count"], 0)

    def test_m_and_a_premium_and_risk_flags(self):
        result = mergers_acquisitions_analysis({"asset_class": "equity", "deals": [{
            "deal_id": "deal-1", "deal_status": "ANNOUNCED",
            "target": "Target Co", "acquirer": "Buyer Co",
            "offer_price_per_share": 120, "unaffected_price_per_share": 100,
            "financing_type": "DEBT", "integration_risk": "HIGH",
            "regulatory_status": "REVIEW", "transaction_value": 1_000_000,
            "source_url": "https://example.org/deal",
            "announced_at_utc": "2026-11-10T12:00:00Z",
        }]})
        deal = result["deals"]["deal-1"]
        self.assertEqual(result["status"], "KNOWN")
        self.assertEqual(deal["offer_premium_pct"], 20.0)
        self.assertIn("debt_financing", deal["review_flags"])
        self.assertIn("high_integration_risk", deal["review_flags"])
        self.assertIn("regulatory_review_or_challenge", deal["review_flags"])

    def test_crypto_m_and_a_is_not_applicable(self):
        result = mergers_acquisitions_analysis({"asset_class": "crypto"})
        self.assertEqual(result["status"], "NOT_APPLICABLE")
        self.assertIsNone(result["score"])


class SectorRotationTests(unittest.TestCase):
    def test_sector_relative_strength_and_leaders(self):
        result = sector_rotation_analysis({
            "economic_phase": {"value": "MID_EXPANSION", "source": "https://example.org/macro",
                               "observed_at_utc": "2026-11-01T00:00:00Z"},
            "sectors": [
                {"sector": "Technology", "return_pct": 12, "benchmark_return_pct": 5,
                 "flow_change_pct": 4, "source": "https://example.org/tech",
                 "observed_at_utc": "2026-11-01T00:00:00Z"},
                {"sector": "Utilities", "return_pct": 2, "benchmark_return_pct": 5,
                 "flow_change_pct": -1, "source": "https://example.org/util",
                 "observed_at_utc": "2026-11-01T00:00:00Z"},
            ]})
        self.assertEqual(result["status"], "KNOWN")
        self.assertEqual(result["economic_phase"], "MID_EXPANSION")
        self.assertEqual(result["leaders"][0], "Technology")
        self.assertEqual(result["laggards"][0], "Utilities")
        self.assertEqual(result["sectors"][0]["relative_strength_pct"], 7)

    def test_missing_sector_source_is_not_counted(self):
        result = sector_rotation_analysis({"sectors": [
            {"sector": "Technology", "return_pct": 10, "benchmark_return_pct": 5}
        ]})
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["sectors"], [])

    def test_diagnostics_do_not_change_weighted_score_or_trade_authorization(self):
        result = build_fundamental_report({
            "mergers_acquisitions": {"asset_class": "crypto"},
            "sector_rotation": {},
        })
        self.assertEqual(set(result["weights"]), set(MODULE_WEIGHTS))
        self.assertEqual(result["modules"]["mergers_acquisitions"]["status"], "NOT_APPLICABLE")
        self.assertEqual(result["modules"]["sector_rotation"]["status"], "UNKNOWN")
        self.assertFalse(result["trade_authorized"])

class EventAnalysisTests(unittest.TestCase):
    def test_missing_events_remain_unknown(self):
        from crypto_super_radar.fundamental_engine import event_analysis
        result = event_analysis({})
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertIsNone(result["score"])

    def test_market_model_computes_abnormal_returns_and_t_test(self):
        from crypto_super_radar.fundamental_engine import event_analysis
        observations = []
        for i in range(30):
            benchmark = (i - 14) * 0.0007
            residual = ((i % 5) - 2) * 0.0004
            actual = 0.0005 + 1.15 * benchmark + residual
            observations.append({"timestamp": f"2026-10-{(i % 28) + 1:02d}T00:00:00Z",
                "asset_return": actual, "benchmark_return": benchmark,
                "window": "ESTIMATION" if i < 25 else "EVENT"})
        for row in observations[25:]: row["asset_return"] += 0.04
        result = event_analysis({"events": [{"event_id": "test_release", "event_type": "protocol_upgrade",
            "event_time_utc": "2026-11-01T12:00:00Z", "source_url": "https://example.org/announcement",
            "event_window": {"before_days": 1, "after_days": 2}, "observations": observations}]})
        event = result["events"]["test_release"]
        self.assertEqual(result["status"], "KNOWN")
        self.assertEqual(event["status"], "KNOWN")
        self.assertEqual(len(event["abnormal_returns"]), 5)
        self.assertGreater(event["cumulative_abnormal_return"], 0)
        self.assertLess(event["p_value_two_sided"], 0.05)
        self.assertTrue(event["statistically_significant_5pct"])

    def test_event_without_timestamped_source_is_unknown(self):
        from crypto_super_radar.fundamental_engine import event_analysis
        result = event_analysis({"events": [{"event_id": "missing_source", "observations": []}]})
        self.assertEqual(result["events"]["missing_source"]["status"], "UNKNOWN")

if __name__ == "__main__":
    unittest.main()
